"""
Single entry point for all background/periodic work. Intended to be invoked by an
external scheduler every 5-10 minutes:

    python manage.py run_scheduled_jobs

On Render: a Cron Job resource running this command on a schedule.
On Railway/Vercel: a scheduled Function or GitHub Actions workflow hitting a
protected management endpoint / running this via `railway run`.

Each sub-task is wrapped individually so one failing task (e.g. LLM retry hitting
a persistent outage) never prevents the others (e.g. medication reminders) from
running in the same cycle.
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.services import booking_service, email_service, llm_service
from core.models import MedicationReminder, Appointment, NotificationLog

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs all periodic background jobs: hold expiry, reminders, and retries."

    def handle(self, *args, **options):
        results = {}

        results["expired_holds_released"] = self._safe_run(booking_service.release_expired_holds)
        results["medication_reminders_sent"] = self._safe_run(self._send_due_medication_reminders)
        results["appointment_reminders_sent"] = self._safe_run(self._send_due_appointment_reminders)
        results["notifications_retried"] = self._safe_run(email_service.retry_failed_notifications)
        results["llm_summaries_retried"] = self._safe_run(llm_service.retry_failed_summaries)

        self.stdout.write(self.style.SUCCESS(f"Background jobs complete: {results}"))

    def _safe_run(self, fn):
        try:
            return fn()
        except Exception as exc:
            logger.error("Background job %s failed: %s", fn.__name__, exc)
            self.stderr.write(self.style.ERROR(f"{fn.__name__} failed: {exc}"))
            return 0

    def _send_due_medication_reminders(self) -> int:
        due = MedicationReminder.objects.filter(
            sent=False, scheduled_at__lte=timezone.now()
        ).select_related("prescription__post_visit_note__appointment__patient__user__auth_user")

        count = 0
        for reminder in due:
            patient_user = reminder.prescription.post_visit_note.appointment.patient.user
            email_service.send_medication_reminder(
                patient_user, reminder.prescription.medication_name, reminder.prescription.dosage
            )
            reminder.sent = True
            reminder.sent_at = timezone.now()
            reminder.save(update_fields=["sent", "sent_at"])
            count += 1
        return count

    def _send_due_appointment_reminders(self, hours_before: int = 24) -> int:
        """Sends a reminder for confirmed appointments starting within the next
        `hours_before` hours, skipping any appointment that already has a 'reminder'
        NotificationLog row so re-running this command doesn't double-send."""
        window_end = timezone.now() + timezone.timedelta(hours=hours_before)
        upcoming = Appointment.objects.filter(
            status="confirmed", slot_start__gt=timezone.now(), slot_start__lte=window_end
        )

        count = 0
        for appt in upcoming:
            already_sent = NotificationLog.objects.filter(
                appointment=appt, notif_type="reminder", status="sent"
            ).exists()
            if already_sent:
                continue
            email_service.send_appointment_reminder(appt)
            count += 1
        return count
