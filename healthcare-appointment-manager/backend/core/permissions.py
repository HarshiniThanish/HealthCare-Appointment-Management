from rest_framework.permissions import BasePermission


def _role(request):
    profile = getattr(request.user, "profile", None)
    return getattr(profile, "role", None)


class IsPatient(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and _role(request) == "patient"


class IsDoctor(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and _role(request) == "doctor"


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and _role(request) == "admin"


class IsAppointmentParticipant(BasePermission):
    """Object-level check: only the patient or doctor on the appointment (or admin)
    may view/act on it."""
    def has_object_permission(self, request, view, obj):
        role = _role(request)
        if role == "admin":
            return True
        if role == "patient":
            return obj.patient.user.auth_user_id == request.user.id
        if role == "doctor":
            return obj.doctor.user.auth_user_id == request.user.id
        return False
