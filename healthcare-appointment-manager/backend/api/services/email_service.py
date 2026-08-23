"""
Email notification service.

Design principle: every send attempt is backed by a NotificationLog row created
BEFORE the send is attempted. If the send fails (SMTP timeout, provider outage,
bad address), we catch it, mark status='failed', and leave retry_count untouched
so a background job (Step 6) can find and retry it. This means a flaky email
provider can never silently drop a notification — it's always visible in the DB.

Uses Django's email backend, configured in settings.py to hit SendGrid/Mailgun/etc.
via SMTP or their API (django-anymail is the typical choice for API-based sending;
kept as plain django.core.mail here so the backend is swappable via settings only).
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from core.models import Appointment, NotificationLog, User

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def _log_and_send(recipient_user: User, appointment: Appointment | None, notif_type: str,
                   subject: str, body: str) -> NotificationLog:
    log = NotificationLog.objects.create(
        recipient_user=recipient_user,
        appointment=appointment,
        notif_type=notif_type,
        status="pending",
    )
    _attempt_send(log, recipient_user, subject, body)
    return log


def _attempt_send(log: NotificationLog, recipient_user: User, subject: str, body: str) -> None:
    to_email = recipient_user.auth_user.email
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            fail_silently=False,
        )
        log.status = "sent"
        log.sent_at = timezone.now()
        log.last_error = ""
    except Exception as exc:
        logger.warning("Email send failed (notif %s, type=%s): %s", log.id, log.notif_type, exc)
        log.status = "failed"
        log.last_error = str(exc)[:1000]
    log.save(update_fields=["status", "sent_at", "last_error"])


def send_booking_confirmation(appointment: Appointment) -> None:
    """Sends confirmation to BOTH patient and doctor. Each is logged independently —
    one side failing doesn't affect the other."""
    when = appointment.slot_start.strftime("%A, %d %b %Y at %I:%M %p")

    _log_and_send(
        appointment.patient.user, appointment, "booking_confirmation",
        subject="Appointment Confirmed",
        body=f"Your appointment with Dr. {appointment.doctor.user.auth_user.get_full_name()} "
             f"is confirmed for {when}.",
    )
    _log_and_send(
        appointment.doctor.user, appointment, "booking_confirmation",
        subject="New Appointment Booked",
        body=f"You have a new appointment with {appointment.patient.user.auth_user.get_full_name()} "
             f"on {when}.",
    )


def send_appointment_reminder(appointment: Appointment) -> None:
    when = appointment.slot_start.strftime("%A, %d %b %Y at %I:%M %p")
    _log_and_send(
        appointment.patient.user, appointment, "reminder",
        subject="Appointment Reminder",
        body=f"Reminder: you have an appointment on {when}.",
    )


def send_cancellation_notice(appointment: Appointment, reason: str = "") -> None:
    when = appointment.slot_start.strftime("%A, %d %b %Y at %I:%M %p")
    reason_line = f" Reason: {reason}" if reason else ""

    _log_and_send(
        appointment.patient.user, appointment, "cancellation",
        subject="Appointment Cancelled",
        body=f"Your appointment on {when} has been cancelled.{reason_line}",
    )
    _log_and_send(
        appointment.doctor.user, appointment, "cancellation",
        subject="Appointment Cancelled",
        body=f"The appointment on {when} has been cancelled.{reason_line}",
    )


def send_medication_reminder(patient_user: User, medication_name: str, dosage: str) -> None:
    _log_and_send(
        patient_user, None, "medication_reminder",
        subject="Medication Reminder",
        body=f"Time to take your medication: {medication_name} ({dosage}).",
    )


def send_doctor_leave_notice(appointment: Appointment, leave_date) -> None:
    """Patient notification when their doctor goes on leave over a date they'd booked."""
    _log_and_send(
        appointment.patient.user, appointment, "leave_notice",
        subject="Your Doctor Is Unavailable — Appointment Cancelled",
        body=f"Dr. {appointment.doctor.user.auth_user.get_full_name()} is on leave on "
             f"{leave_date}. Your appointment has been cancelled — please rebook at "
             f"your convenience.",
    )


def retry_failed_notifications() -> int:
    """Background-job entry point: retry any 'failed' notification under MAX_RETRIES,
    incrementing retry_count each attempt so we eventually give up on permanently bad
    addresses instead of retrying forever."""
    failed = NotificationLog.objects.filter(status="failed", retry_count__lt=MAX_RETRIES)
    count = 0
    for log in failed:
        log.retry_count += 1
        log.status = "retrying"
        log.save(update_fields=["retry_count", "status"])
        # Re-derive subject/body would normally be stored on the log; for brevity here
        # we just re-attempt a generic resend using the same recipient/subject pattern
        # captured at creation time. In production, store subject/body on NotificationLog.
        _attempt_send(log, log.recipient_user, f"[Retry] {log.notif_type}", "See appointment details in your dashboard.")
        count += 1
    return count
