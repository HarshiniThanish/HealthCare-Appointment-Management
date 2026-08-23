from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from core.models import (
    User, PatientProfile, DoctorProfile, Appointment, SymptomForm,
    PreVisitSummary, PostVisitNote, PostVisitSummary, Prescription,
)

AuthUser = get_user_model()


# ---------------------------------------------------------------------------
# Auth / Registration
# ---------------------------------------------------------------------------

class RoleTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Embeds role + display name into the JWT itself, so the frontend can route
    to the right portal immediately after login without a follow-up 'me' call."""
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        profile = getattr(user, "profile", None)
        token["role"] = getattr(profile, "role", None)
        token["name"] = user.get_full_name() or user.username
        return token


class PatientRegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    gender = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_username(self, value):
        if AuthUser.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username already taken.")
        return value

    def create(self, validated_data):
        auth_user = AuthUser.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
        )
        profile = User.objects.create(
            auth_user=auth_user, role="patient", phone=validated_data.get("phone", "")
        )
        PatientProfile.objects.create(
            user=profile,
            date_of_birth=validated_data.get("date_of_birth"),
            gender=validated_data.get("gender", ""),
        )
        return profile


# Doctor accounts are created by admin only (see DoctorProfile admin flow), not
# self-registration — matches "Admin creates and manages doctor profiles" in the spec.
class DoctorCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    specialization = serializers.CharField(max_length=120)
    bio = serializers.CharField(required=False, allow_blank=True)
    slot_duration_minutes = serializers.IntegerField(default=30, min_value=5, max_value=180)
    consultation_fee = serializers.DecimalField(max_digits=8, decimal_places=2, required=False, allow_null=True)

    def create(self, validated_data):
        auth_user = AuthUser.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
        )
        profile = User.objects.create(auth_user=auth_user, role="doctor")
        return DoctorProfile.objects.create(
            user=profile,
            specialization=validated_data["specialization"],
            bio=validated_data.get("bio", ""),
            slot_duration_minutes=validated_data.get("slot_duration_minutes", 30),
            consultation_fee=validated_data.get("consultation_fee"),
        )


# ---------------------------------------------------------------------------
# Doctor search
# ---------------------------------------------------------------------------

class DoctorListSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.auth_user.get_full_name", read_only=True)
    username = serializers.CharField(source="user.auth_user.username", read_only=True)

    class Meta:
        model = DoctorProfile
        fields = ["id", "name", "username", "specialization", "bio",
                  "slot_duration_minutes", "consultation_fee", "is_active"]


class AvailableSlotsQuerySerializer(serializers.Serializer):
    date = serializers.DateField()


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class HoldSlotSerializer(serializers.Serializer):
    doctor_id = serializers.IntegerField()
    slot_start = serializers.DateTimeField()


class SymptomFormSerializer(serializers.ModelSerializer):
    class Meta:
        model = SymptomForm
        fields = ["id", "appointment", "symptoms_text", "submitted_at"]
        read_only_fields = ["id", "appointment", "submitted_at"]


class PreVisitSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = PreVisitSummary
        fields = ["id", "status", "urgency_level", "chief_complaint",
                  "suggested_questions", "error_message", "generated_at"]


class PostVisitSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = PostVisitSummary
        fields = ["id", "status", "patient_friendly_summary",
                  "medication_schedule", "follow_up_steps", "error_message", "generated_at"]


class PrescriptionCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prescription
        fields = ["medication_name", "dosage", "times_per_day", "duration_days", "start_date", "notes"]


class PostVisitNoteCreateSerializer(serializers.ModelSerializer):
    prescriptions = PrescriptionCreateSerializer(many=True, required=False)

    class Meta:
        model = PostVisitNote
        fields = ["clinical_notes", "prescription_text", "prescriptions"]

    def create(self, validated_data):
        prescriptions_data = validated_data.pop("prescriptions", [])
        appointment = validated_data.pop("appointment")
        note = PostVisitNote.objects.create(appointment=appointment, **validated_data)
        for p in prescriptions_data:
            Prescription.objects.create(post_visit_note=note, **p)
        return note


class PostVisitNoteSerializer(serializers.ModelSerializer):
    prescriptions = PrescriptionCreateSerializer(many=True, read_only=True)

    class Meta:
        model = PostVisitNote
        fields = ["id", "appointment", "clinical_notes", "prescription_text",
                  "prescriptions", "submitted_at"]


class AppointmentSerializer(serializers.ModelSerializer):
    doctor_name = serializers.CharField(source="doctor.user.auth_user.get_full_name", read_only=True)
    patient_name = serializers.CharField(source="patient.user.auth_user.get_full_name", read_only=True)

    class Meta:
        model = Appointment
        fields = ["id", "patient", "patient_name", "doctor", "doctor_name",
                  "slot_start", "slot_end", "status", "hold_expires_at",
                  "cancellation_reason", "created_at"]
        read_only_fields = ["id", "status", "hold_expires_at", "created_at"]
