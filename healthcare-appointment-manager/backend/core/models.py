"""
Healthcare Appointment & Follow-up Manager — Core Data Models
Django ORM models. App: `core`

Design notes (see README for full write-up):
- Double-booking prevented via UniqueConstraint(doctor, slot_start) + select_for_update()
  at booking time (transactional row lock).
- Slots are computed on the fly from DoctorWorkingHours + slot_duration, minus existing
  Appointments and DoctorLeave — no pre-generated slot table.
- LLM-derived fields (PreVisitSummary, PostVisitSummary) carry a `status` enum so a failed
  LLM call never blocks booking or the clinical workflow.
- NotificationLog gives every email a durable row with retry_count, so a background job
  can safely retry failed sends without duplicating successful ones.
"""

from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


# ---------------------------------------------------------------------------
# Users & Roles
# ---------------------------------------------------------------------------

class User(models.Model):
    """
    Extends Django's built-in auth via a OneToOne, rather than a custom User model,
    so we keep Django's battle-tested auth/password/session machinery untouched.
    """
    ROLE_CHOICES = [
        ("patient", "Patient"),
        ("doctor", "Doctor"),
        ("admin", "Admin"),
    ]
    auth_user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, db_index=True)
    phone = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.auth_user.get_username()} ({self.role})"


class PatientProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="patient_profile")
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)


class DoctorProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="doctor_profile")
    specialization = models.CharField(max_length=120, db_index=True)
    bio = models.TextField(blank=True)
    slot_duration_minutes = models.PositiveIntegerField(default=30)
    consultation_fee = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)  # admin can deactivate a doctor profile


# ---------------------------------------------------------------------------
# Availability & Leave
# ---------------------------------------------------------------------------

class DoctorWorkingHours(models.Model):
    """Recurring weekly availability, e.g. Mon 09:00-13:00."""
    DAY_CHOICES = [(i, day) for i, day in enumerate(
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]

    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="working_hours")
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(end_time__gt=models.F("start_time")),
                                    name="working_hours_end_after_start")
        ]
        indexes = [models.Index(fields=["doctor", "day_of_week"])]


class DoctorLeave(models.Model):
    """A single day the doctor is unavailable. Triggers cancellation flow for existing bookings."""
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="leave_days")
    leave_date = models.DateField(db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["doctor", "leave_date"], name="unique_doctor_leave_date")
        ]


# ---------------------------------------------------------------------------
# Appointments — the core booking entity
# ---------------------------------------------------------------------------

class Appointment(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),        # slot held, symptom form not yet submitted
        ("confirmed", "Confirmed"),
        ("cancelled", "Cancelled"),
        ("completed", "Completed"),
    ]

    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="appointments")
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="appointments")
    slot_start = models.DateTimeField(db_index=True)
    slot_end = models.DateTimeField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending", db_index=True)
    # "pending" = slot held but symptom form not yet submitted. hold_expires_at lets a
    # background job release abandoned holds (e.g. after 10 minutes) so the slot frees up
    # without needing a separate Redis-based hold layer. See system design write-up.
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # The heart of double-booking prevention: no two active appointments
            # can share the same doctor + start time at the DB level.
            models.UniqueConstraint(
                fields=["doctor", "slot_start"],
                condition=models.Q(status__in=["pending", "confirmed"]),
                name="unique_active_doctor_slot",
            )
        ]
        indexes = [models.Index(fields=["doctor", "slot_start", "status"])]


# ---------------------------------------------------------------------------
# Pre-visit: symptoms + LLM summary
# ---------------------------------------------------------------------------

class SymptomForm(models.Model):
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name="symptom_form")
    symptoms_text = models.TextField()
    submitted_at = models.DateTimeField(auto_now_add=True)


class PreVisitSummary(models.Model):
    URGENCY_CHOICES = [("Low", "Low"), ("Medium", "Medium"), ("High", "High")]
    LLM_STATUS = [("pending", "Pending"), ("success", "Success"), ("failed", "Failed")]

    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name="pre_visit_summary")
    status = models.CharField(max_length=10, choices=LLM_STATUS, default="pending")
    urgency_level = models.CharField(max_length=6, choices=URGENCY_CHOICES, null=True, blank=True)
    chief_complaint = models.TextField(blank=True)
    suggested_questions = models.JSONField(default=list, blank=True)  # list[str], length 3
    raw_llm_response = models.TextField(blank=True)  # stored for debugging/audit
    error_message = models.TextField(blank=True)     # populated if status == failed
    generated_at = models.DateTimeField(null=True, blank=True)


# ---------------------------------------------------------------------------
# Post-visit: doctor notes + LLM patient-friendly summary
# ---------------------------------------------------------------------------

class PostVisitNote(models.Model):
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name="post_visit_note")
    clinical_notes = models.TextField()
    prescription_text = models.TextField(blank=True)  # free-text as entered by doctor
    submitted_at = models.DateTimeField(auto_now_add=True)


class PostVisitSummary(models.Model):
    LLM_STATUS = [("pending", "Pending"), ("success", "Success"), ("failed", "Failed")]

    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name="post_visit_summary")
    status = models.CharField(max_length=10, choices=LLM_STATUS, default="pending")
    patient_friendly_summary = models.TextField(blank=True)
    medication_schedule = models.JSONField(default=list, blank=True)  # structured, see Prescription
    follow_up_steps = models.TextField(blank=True)
    raw_llm_response = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)


class Prescription(models.Model):
    """Structured medication rows, parsed/entered alongside the post-visit note.
    Drives the medication reminder background job."""
    post_visit_note = models.ForeignKey(PostVisitNote, on_delete=models.CASCADE, related_name="prescriptions")
    medication_name = models.CharField(max_length=200)
    dosage = models.CharField(max_length=100)  # e.g. "500mg"
    times_per_day = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(6)])
    duration_days = models.PositiveIntegerField()
    start_date = models.DateField()
    notes = models.CharField(max_length=255, blank=True)  # e.g. "after food"


class MedicationReminder(models.Model):
    """One row per scheduled reminder occurrence. Generated by a background job
    when a Prescription is created; consumed by the reminder-sending job."""
    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="reminders")
    scheduled_at = models.DateTimeField(db_index=True)
    sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["scheduled_at", "sent"])]


# ---------------------------------------------------------------------------
# Notifications (email) — durable log for retry safety
# ---------------------------------------------------------------------------

class NotificationLog(models.Model):
    NOTIF_TYPES = [
        ("booking_confirmation", "Booking Confirmation"),
        ("reminder", "Appointment Reminder"),
        ("medication_reminder", "Medication Reminder"),
        ("cancellation", "Cancellation"),
        ("leave_notice", "Doctor Leave Notice"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("retrying", "Retrying"),
    ]

    recipient_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, null=True, blank=True,
                                     related_name="notifications")
    notif_type = models.CharField(max_length=30, choices=NOTIF_TYPES)
    channel = models.CharField(max_length=10, default="email")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending", db_index=True)
    retry_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "retry_count"])]


# ---------------------------------------------------------------------------
# Google Calendar linkage
# ---------------------------------------------------------------------------

class GoogleCalendarCredential(models.Model):
    """OAuth2 tokens for a user's Google account, obtained via the standard
    authorization-code flow. Refresh token lets us silently renew access without
    re-prompting the user. One row per user (patient or doctor) who has connected
    their calendar."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="google_credential")
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expiry = models.DateTimeField()
    scope = models.CharField(max_length=255, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)


class CalendarEvent(models.Model):
    """Tracks the Google Calendar event IDs created for each side of an appointment,
    so we know exactly what to update/delete on reschedule or cancellation."""
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name="calendar_event")
    patient_event_id = models.CharField(max_length=255, blank=True)
    doctor_event_id = models.CharField(max_length=255, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)
