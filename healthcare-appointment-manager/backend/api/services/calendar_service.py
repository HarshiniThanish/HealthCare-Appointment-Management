"""
Google Calendar integration — OAuth2 connect flow + event create/update/delete on
booking, reschedule, and cancellation.

Design principle: calendar sync is best-effort and NEVER blocks the booking flow.
Each side (patient/doctor) is synced independently and failures are recorded on
CalendarEvent.sync_error rather than raised — a patient without a connected Google
account, or a transient Google API error, should never prevent a booking from
succeeding.
"""

import logging
from datetime import datetime

from django.conf import settings
from django.utils import timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.models import Appointment, CalendarEvent, GoogleCalendarCredential, User

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CALENDAR_ID = "primary"


class CalendarNotConnected(Exception):
    pass


# ---------------------------------------------------------------------------
# OAuth2 connect flow
# ---------------------------------------------------------------------------

def build_auth_flow(redirect_uri: str) -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def get_authorization_url(redirect_uri: str) -> str:
    flow = build_auth_flow(redirect_uri)
    auth_url, _state = flow.authorization_url(access_type="offline", prompt="consent")
    return auth_url


def save_credentials_from_callback(user: User, redirect_uri: str, callback_url: str) -> None:
    """Exchanges the OAuth2 code (embedded in callback_url's query string) for tokens
    and persists them for this user."""
    flow = build_auth_flow(redirect_uri)
    flow.fetch_token(authorization_response=callback_url)
    creds = flow.credentials

    GoogleCalendarCredential.objects.update_or_create(
        user=user,
        defaults={
            "access_token": creds.token,
            "refresh_token": creds.refresh_token or "",
            "token_expiry": creds.expiry,
            "scope": " ".join(creds.scopes or SCOPES),
        },
    )


def _get_service_for_user(user: User):
    """Builds an authenticated Calendar API client for the given user, refreshing
    the access token if expired. Raises CalendarNotConnected if the user never
    linked their Google account — callers catch this and skip that side's sync."""
    try:
        cred_row = user.google_credential
    except GoogleCalendarCredential.DoesNotExist:
        raise CalendarNotConnected(f"User {user.id} has not connected Google Calendar.")

    creds = Credentials(
        token=cred_row.access_token,
        refresh_token=cred_row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        cred_row.access_token = creds.token
        cred_row.token_expiry = creds.expiry
        cred_row.save(update_fields=["access_token", "token_expiry"])

    return build("calendar", "v3", credentials=creds)


def _event_body(appointment: Appointment) -> dict:
    return {
        "summary": f"Appointment: {appointment.patient.user.auth_user.get_full_name()} "
                   f"with Dr. {appointment.doctor.user.auth_user.get_full_name()}",
        "start": {"dateTime": appointment.slot_start.isoformat()},
        "end": {"dateTime": appointment.slot_end.isoformat()},
    }


# ---------------------------------------------------------------------------
# Sync operations
# ---------------------------------------------------------------------------

def create_calendar_events(appointment: Appointment) -> CalendarEvent:
    """Creates a Calendar event on each connected side. Never raises."""
    cal_event, _ = CalendarEvent.objects.get_or_create(appointment=appointment)
    errors = []

    for side, user, field in (
        ("patient", appointment.patient.user, "patient_event_id"),
        ("doctor", appointment.doctor.user, "doctor_event_id"),
    ):
        try:
            service = _get_service_for_user(user)
            event = service.events().insert(calendarId=CALENDAR_ID, body=_event_body(appointment)).execute()
            setattr(cal_event, field, event["id"])
        except CalendarNotConnected:
            logger.info("Skipping calendar sync for %s side — not connected.", side)
        except HttpError as exc:
            logger.warning("Calendar API error for %s side, appointment %s: %s", side, appointment.id, exc)
            errors.append(f"{side}: {exc}")
        except Exception as exc:
            logger.warning("Unexpected calendar sync error for %s side: %s", side, exc)
            errors.append(f"{side}: {exc}")

    cal_event.sync_error = "; ".join(errors)
    cal_event.last_synced_at = timezone.now()
    cal_event.save()
    return cal_event


def update_calendar_events(appointment: Appointment) -> CalendarEvent:
    """Reschedule: patch existing events' start/end times."""
    cal_event, created = CalendarEvent.objects.get_or_create(appointment=appointment)
    if created:
        return create_calendar_events(appointment)

    errors = []
    for side, user, field in (
        ("patient", appointment.patient.user, cal_event.patient_event_id),
        ("doctor", appointment.doctor.user, cal_event.doctor_event_id),
    ):
        if not field:
            continue
        try:
            service = _get_service_for_user(user)
            service.events().patch(
                calendarId=CALENDAR_ID, eventId=field,
                body={"start": {"dateTime": appointment.slot_start.isoformat()},
                      "end": {"dateTime": appointment.slot_end.isoformat()}},
            ).execute()
        except CalendarNotConnected:
            pass
        except Exception as exc:
            logger.warning("Calendar update failed for %s side: %s", side, exc)
            errors.append(f"{side}: {exc}")

    cal_event.sync_error = "; ".join(errors)
    cal_event.last_synced_at = timezone.now()
    cal_event.save()
    return cal_event


def delete_calendar_events(appointment: Appointment) -> None:
    """Cancellation: remove both sides' events. Best-effort — logs and moves on."""
    try:
        cal_event = appointment.calendar_event
    except CalendarEvent.DoesNotExist:
        return

    for side, user, field in (
        ("patient", appointment.patient.user, cal_event.patient_event_id),
        ("doctor", appointment.doctor.user, cal_event.doctor_event_id),
    ):
        if not field:
            continue
        try:
            service = _get_service_for_user(user)
            service.events().delete(calendarId=CALENDAR_ID, eventId=field).execute()
        except CalendarNotConnected:
            pass
        except Exception as exc:
            logger.warning("Calendar delete failed for %s side: %s", side, exc)

    cal_event.patient_event_id = ""
    cal_event.doctor_event_id = ""
    cal_event.last_synced_at = timezone.now()
    cal_event.save()
