"""
Booking service — slot computation and the concurrency-safe booking transaction.

This is the piece the assignment cares most about: "prevent double-booking and
handle simultaneous booking attempts safely."

Strategy:
1. Slots are computed on demand (not stored), from DoctorWorkingHours minus
   DoctorLeave minus existing active Appointments.
2. Booking a slot happens inside a single DB transaction that:
   a. Takes a row lock (SELECT ... FOR UPDATE) on any existing appointment
      for that exact (doctor, slot_start).
   b. Re-validates the slot is still inside working hours and not on a leave day
      (guards against a race where leave was added between slot-list and booking).
   c. Creates the Appointment row, relying on the DB UniqueConstraint as the final
      backstop even if two transactions somehow interleave around the lock.

Two concurrent requests for the same slot: the second transaction blocks on the
row lock until the first commits or rolls back. If the first succeeded, the second
sees the now-existing row and returns SlotUnavailable — cleanly, no partial state,
no duplicate booking.
"""

from datetime import datetime, timedelta, date as date_cls

from django.db import transaction, IntegrityError
from django.utils import timezone

from core.models import (
    Appointment,
    DoctorProfile,
    DoctorWorkingHours,
    DoctorLeave,
    PatientProfile,
)

HOLD_DURATION_MINUTES = 10  # how long a "pending" (unconfirmed) slot is held


class SlotUnavailable(Exception):
    pass


class OutsideWorkingHours(Exception):
    pass


def get_available_slots(doctor: DoctorProfile, target_date: date_cls) -> list[datetime]:
    """Compute open slots for a doctor on a given date."""
    # 1. Doctor on leave that day -> no slots at all.
    if DoctorLeave.objects.filter(doctor=doctor, leave_date=target_date).exists():
        return []

    # 2. Working hours for that weekday (Monday=0 .. Sunday=6, matching Python's .weekday()).
    day_of_week = target_date.weekday()
    working_blocks = DoctorWorkingHours.objects.filter(doctor=doctor, day_of_week=day_of_week)
    if not working_blocks.exists():
        return []

    slot_len = timedelta(minutes=doctor.slot_duration_minutes)
    candidate_slots: list[datetime] = []

    for block in working_blocks:
        cursor = timezone.make_aware(datetime.combine(target_date, block.start_time))
        block_end = timezone.make_aware(datetime.combine(target_date, block.end_time))
        while cursor + slot_len <= block_end:
            candidate_slots.append(cursor)
            cursor += slot_len

    # 3. Drop slots already taken by an active appointment.
    taken = set(
        Appointment.objects.filter(
            doctor=doctor,
            slot_start__date=target_date,
            status__in=["pending", "confirmed"],
        ).values_list("slot_start", flat=True)
    )
    available = [s for s in candidate_slots if s not in taken]

    # 4. Drop past slots if target_date is today.
    now = timezone.now()
    available = [s for s in available if s > now]

    return available


def _slot_is_valid(doctor: DoctorProfile, slot_start: datetime) -> bool:
    """Re-validate a requested slot against working hours + leave, inside the booking
    transaction, to guard against races with slot generation happening earlier."""
    target_date = slot_start.date()
    if DoctorLeave.objects.filter(doctor=doctor, leave_date=target_date).exists():
        return False
    day_of_week = target_date.weekday()
    slot_time = slot_start.time()
    return DoctorWorkingHours.objects.filter(
        doctor=doctor,
        day_of_week=day_of_week,
        start_time__lte=slot_time,
        end_time__gt=slot_time,
    ).exists()


@transaction.atomic
def hold_slot(patient: PatientProfile, doctor: DoctorProfile, slot_start: datetime) -> Appointment:
    """
    Attempt to reserve (hold) a slot for a patient. This is the concurrency-critical path.
    Raises SlotUnavailable or OutsideWorkingHours on failure; caller (the view) translates
    these into the appropriate HTTP response.
    """
    slot_end = slot_start + timedelta(minutes=doctor.slot_duration_minutes)

    # Lock any existing active appointment row for this exact doctor+slot_start.
    # If another transaction is mid-booking the same slot, this blocks here until
    # it commits/rolls back, then re-checks — no lost updates.
    existing = (
        Appointment.objects.select_for_update()
        .filter(doctor=doctor, slot_start=slot_start, status__in=["pending", "confirmed"])
    )
    if existing.exists():
        raise SlotUnavailable(f"Slot {slot_start.isoformat()} is no longer available.")

    if not _slot_is_valid(doctor, slot_start):
        raise OutsideWorkingHours("Requested slot is outside working hours or on a leave day.")

    try:
        appointment = Appointment.objects.create(
            patient=patient,
            doctor=doctor,
            slot_start=slot_start,
            slot_end=slot_end,
            status="pending",
            hold_expires_at=timezone.now() + timedelta(minutes=HOLD_DURATION_MINUTES),
        )
    except IntegrityError:
        # Final backstop: the DB-level UniqueConstraint caught a race the row lock missed
        # (e.g. two doctor records momentarily out of sync in a multi-instance deploy).
        raise SlotUnavailable(f"Slot {slot_start.isoformat()} is no longer available.")

    return appointment


@transaction.atomic
def confirm_booking(appointment: Appointment) -> Appointment:
    """Called after the patient submits the symptom form. Moves pending -> confirmed.
    Downstream side effects (LLM summary, email, calendar) are triggered by the caller,
    not here, so this function stays a pure state transition and easy to test."""
    locked = Appointment.objects.select_for_update().get(pk=appointment.pk)
    if locked.status != "pending":
        raise ValueError(f"Cannot confirm appointment in status '{locked.status}'.")
    locked.status = "confirmed"
    locked.hold_expires_at = None
    locked.save(update_fields=["status", "hold_expires_at"])
    return locked


def release_expired_holds() -> int:
    """Background-job entry point: cancel 'pending' appointments whose hold has expired
    without a symptom form submission, freeing the slot. Returns count released."""
    expired = Appointment.objects.filter(
        status="pending", hold_expires_at__lt=timezone.now()
    )
    count = expired.count()
    expired.update(status="cancelled", cancellation_reason="Hold expired — symptom form not submitted in time.")
    return count
