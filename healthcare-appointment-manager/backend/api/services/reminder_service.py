"""
Turns a Prescription into concrete MedicationReminder rows the background job can
send at the right time. Kept deliberately simple: doses are spread evenly across a
14-hour waking window (08:00-22:00) each day for the prescription's duration.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from core.models import Prescription, MedicationReminder

DAY_START_HOUR = 8
DAY_END_HOUR = 22


def generate_reminder_schedule(prescription: Prescription) -> int:
    """Creates MedicationReminder rows for the full course. Returns count created."""
    window_hours = DAY_END_HOUR - DAY_START_HOUR
    times_per_day = max(prescription.times_per_day, 1)
    interval = window_hours / times_per_day if times_per_day > 1 else 0

    reminders = []
    for day_offset in range(prescription.duration_days):
        day = prescription.start_date + timedelta(days=day_offset)
        for dose_index in range(times_per_day):
            hour = DAY_START_HOUR + round(dose_index * interval)
            hour = min(hour, DAY_END_HOUR)
            scheduled_at = timezone.make_aware(datetime.combine(day, datetime.min.time())) \
                + timedelta(hours=hour)
            reminders.append(MedicationReminder(prescription=prescription, scheduled_at=scheduled_at))

    MedicationReminder.objects.bulk_create(reminders)
    return len(reminders)
