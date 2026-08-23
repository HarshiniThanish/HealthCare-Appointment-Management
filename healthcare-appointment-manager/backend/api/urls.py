from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    # Auth
    path("auth/register/patient/", views.PatientRegisterView.as_view(), name="register-patient"),
    path("auth/login/", views.RoleTokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token-refresh"),

    # Admin: doctor management
    path("admin/doctors/", views.DoctorCreateView.as_view(), name="doctor-create"),
    path("admin/doctors/<int:doctor_id>/leave/", views.MarkDoctorLeaveView.as_view(), name="doctor-leave"),

    # Doctor search
    path("doctors/", views.DoctorSearchView.as_view(), name="doctor-search"),
    path("doctors/<int:doctor_id>/slots/", views.DoctorAvailableSlotsView.as_view(), name="doctor-slots"),

    # Booking flow
    path("appointments/hold/", views.HoldSlotView.as_view(), name="appointment-hold"),
    path("appointments/<int:appointment_id>/symptoms/", views.SubmitSymptomFormView.as_view(),
         name="appointment-symptoms"),
    path("appointments/<int:appointment_id>/pre-visit-summary/", views.DoctorPreVisitSummaryView.as_view(),
         name="appointment-pre-visit-summary"),
    path("appointments/<int:appointment_id>/post-visit-notes/", views.SubmitPostVisitNoteView.as_view(),
         name="appointment-post-visit-notes"),
    path("appointments/<int:appointment_id>/post-visit-summary/", views.PostVisitSummaryView.as_view(),
         name="appointment-post-visit-summary"),
    path("appointments/mine/", views.MyAppointmentsView.as_view(), name="appointments-mine"),
]
