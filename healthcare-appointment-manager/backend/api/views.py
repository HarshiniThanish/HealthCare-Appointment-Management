from datetime import datetime

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from core.models import DoctorProfile, PatientProfile, Appointment
from core.permissions import IsPatient, IsDoctor, IsAdminRole, IsAppointmentParticipant
from .serializers import (
    RoleTokenObtainPairSerializer,
    PatientRegisterSerializer,
    DoctorCreateSerializer,
    DoctorListSerializer,
    HoldSlotSerializer,
    SymptomFormSerializer,
    AppointmentSerializer,
    PreVisitSummarySerializer,
    PostVisitNoteCreateSerializer,
    PostVisitNoteSerializer,
    PostVisitSummarySerializer,
)
from .services import booking_service, llm_service, email_service, calendar_service, reminder_service
from .services.booking_service import SlotUnavailable, OutsideWorkingHours


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class RoleTokenObtainPairView(TokenObtainPairView):
    serializer_class = RoleTokenObtainPairSerializer


class PatientRegisterView(generics.CreateAPIView):
    permission_classes = [AllowAny]
    serializer_class = PatientRegisterSerializer


class DoctorCreateView(generics.CreateAPIView):
    """Admin-only: create a doctor account + profile."""
    permission_classes = [IsAuthenticated, IsAdminRole]
    serializer_class = DoctorCreateSerializer


# Note: JWT login itself uses simplejwt's built-in TokenObtainPairView,
# wired directly in urls.py — no custom view needed there.


# ---------------------------------------------------------------------------
# Doctor search
# ---------------------------------------------------------------------------

class DoctorSearchView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DoctorListSerializer

    def get_queryset(self):
        qs = DoctorProfile.objects.filter(is_active=True)
        specialization = self.request.query_params.get("specialization")
        if specialization:
            qs = qs.filter(specialization__icontains=specialization)
        return qs.select_related("user__auth_user")


class DoctorAvailableSlotsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, doctor_id):
        date_str = request.query_params.get("date")
        if not date_str:
            return Response({"detail": "Query param 'date' (YYYY-MM-DD) is required."},
                             status=status.HTTP_400_BAD_REQUEST)
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format, expected YYYY-MM-DD."},
                             status=status.HTTP_400_BAD_REQUEST)

        try:
            doctor = DoctorProfile.objects.get(pk=doctor_id, is_active=True)
        except DoctorProfile.DoesNotExist:
            return Response({"detail": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)

        slots = booking_service.get_available_slots(doctor, target_date)
        return Response({"doctor_id": doctor.id, "date": date_str,
                          "available_slots": [s.isoformat() for s in slots]})


# ---------------------------------------------------------------------------
# Booking flow: hold -> submit symptom form -> confirmed
# ---------------------------------------------------------------------------

class HoldSlotView(APIView):
    """Step 1 of booking: reserve a slot. This is the concurrency-critical endpoint —
    see booking_service.hold_slot for the transaction/locking details."""
    permission_classes = [IsAuthenticated, IsPatient]

    def post(self, request):
        serializer = HoldSlotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            doctor = DoctorProfile.objects.get(pk=data["doctor_id"], is_active=True)
        except DoctorProfile.DoesNotExist:
            return Response({"detail": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)

        patient = PatientProfile.objects.get(user=request.user.profile)

        try:
            appointment = booking_service.hold_slot(patient, doctor, data["slot_start"])
        except SlotUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except OutsideWorkingHours as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(AppointmentSerializer(appointment).data, status=status.HTTP_201_CREATED)


class SubmitSymptomFormView(APIView):
    """Step 2 of booking: patient submits symptoms, which confirms the appointment
    and (in the next build phase) triggers the pre-visit LLM summary + email + calendar."""
    permission_classes = [IsAuthenticated, IsPatient, IsAppointmentParticipant]

    def post(self, request, appointment_id):
        try:
            appointment = Appointment.objects.select_related("patient__user", "doctor__user").get(
                pk=appointment_id
            )
        except Appointment.DoesNotExist:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)

        self.check_object_permissions(request, appointment)

        symptoms_text = request.data.get("symptoms_text", "").strip()
        if not symptoms_text:
            return Response({"detail": "symptoms_text is required."}, status=status.HTTP_400_BAD_REQUEST)

        from core.models import SymptomForm
        if hasattr(appointment, "symptom_form"):
            return Response({"detail": "Symptom form already submitted for this appointment."},
                             status=status.HTTP_400_BAD_REQUEST)

        SymptomForm.objects.create(appointment=appointment, symptoms_text=symptoms_text)

        try:
            confirmed = booking_service.confirm_booking(appointment)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Generate the pre-visit AI summary now. llm_service never raises — a failure
        # is recorded on the PreVisitSummary row (status='failed') and does not affect
        # this response. In production this call would instead be queued
        # (generate_pre_visit_summary.delay(confirmed.id)) so the API responds instantly;
        # kept synchronous here for clarity — see Step 6 (background jobs).
        llm_service.generate_pre_visit_summary(confirmed)

        # Both are best-effort and never raise — a bad email address or a patient who
        # never connected Google Calendar must not roll back a successful booking.
        email_service.send_booking_confirmation(confirmed)
        calendar_service.create_calendar_events(confirmed)

        return Response(AppointmentSerializer(confirmed).data, status=status.HTTP_200_OK)


class DoctorPreVisitSummaryView(APIView):
    """Doctor views the AI-generated pre-visit summary before the appointment."""
    permission_classes = [IsAuthenticated, IsDoctor, IsAppointmentParticipant]

    def get(self, request, appointment_id):
        appointment = _get_appointment_or_404(appointment_id)
        if appointment is None:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, appointment)

        summary = getattr(appointment, "pre_visit_summary", None)
        if summary is None:
            return Response({"detail": "No pre-visit summary yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PreVisitSummarySerializer(summary).data)


class SubmitPostVisitNoteView(APIView):
    """Doctor submits clinical notes + prescriptions after the visit. Triggers the
    patient-friendly post-visit LLM summary."""
    permission_classes = [IsAuthenticated, IsDoctor, IsAppointmentParticipant]

    def post(self, request, appointment_id):
        appointment = _get_appointment_or_404(appointment_id)
        if appointment is None:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, appointment)

        if hasattr(appointment, "post_visit_note"):
            return Response({"detail": "Post-visit note already submitted."},
                             status=status.HTTP_400_BAD_REQUEST)

        serializer = PostVisitNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.save(appointment=appointment)

        appointment.status = "completed"
        appointment.save(update_fields=["status"])

        # Same never-raises guarantee as the pre-visit path.
        llm_service.generate_post_visit_summary(appointment)

        for prescription in note.prescriptions.all():
            reminder_service.generate_reminder_schedule(prescription)

        return Response(PostVisitNoteSerializer(note).data, status=status.HTTP_201_CREATED)


class PostVisitSummaryView(APIView):
    """Patient views the AI-generated, patient-friendly post-visit summary."""
    permission_classes = [IsAuthenticated, IsPatient, IsAppointmentParticipant]

    def get(self, request, appointment_id):
        appointment = _get_appointment_or_404(appointment_id)
        if appointment is None:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, appointment)

        summary = getattr(appointment, "post_visit_summary", None)
        if summary is None:
            return Response({"detail": "No post-visit summary yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PostVisitSummarySerializer(summary).data)


def _get_appointment_or_404(appointment_id):
    try:
        return Appointment.objects.select_related("patient__user", "doctor__user").get(pk=appointment_id)
    except Appointment.DoesNotExist:
        return None


class MarkDoctorLeaveView(APIView):
    """Admin marks a doctor on leave for a date. Any existing active appointments on
    that date are cancelled, patients are emailed, and calendar events removed —
    this is the 'leave conflict handling' requirement from the spec, kept as one
    atomic transaction so we never end up with a leave day + a still-active booking."""
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request, doctor_id):
        from django.db import transaction
        from core.models import DoctorLeave

        leave_date = request.data.get("leave_date")
        reason = request.data.get("reason", "")
        if not leave_date:
            return Response({"detail": "leave_date is required (YYYY-MM-DD)."},
                             status=status.HTTP_400_BAD_REQUEST)

        try:
            doctor = DoctorProfile.objects.get(pk=doctor_id)
        except DoctorProfile.DoesNotExist:
            return Response({"detail": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            leave, _ = DoctorLeave.objects.get_or_create(
                doctor=doctor, leave_date=leave_date, defaults={"reason": reason}
            )
            affected = Appointment.objects.select_for_update().filter(
                doctor=doctor, slot_start__date=leave_date, status__in=["pending", "confirmed"]
            )
            affected_list = list(affected)
            affected.update(status="cancelled", cancellation_reason=f"Doctor on leave: {reason or 'unspecified'}")

        # Side effects run after the transaction commits — email/calendar failures
        # must not roll back the cancellation itself.
        for appt in affected_list:
            email_service.send_doctor_leave_notice(appt, leave_date)
            calendar_service.delete_calendar_events(appt)

        return Response({
            "leave_id": leave.id,
            "affected_appointments": [a.id for a in affected_list],
        }, status=status.HTTP_201_CREATED)


class MyAppointmentsView(generics.ListAPIView):
    """Patients see their own bookings; doctors see their schedule."""
    permission_classes = [IsAuthenticated]
    serializer_class = AppointmentSerializer

    def get_queryset(self):
        profile = self.request.user.profile
        if profile.role == "patient":
            return Appointment.objects.filter(patient__user=profile).order_by("-slot_start")
        if profile.role == "doctor":
            return Appointment.objects.filter(doctor__user=profile).order_by("-slot_start")
        return Appointment.objects.none()
