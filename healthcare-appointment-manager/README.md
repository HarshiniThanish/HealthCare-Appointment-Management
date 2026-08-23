# Healthcare Appointment & Follow-up Manager

Full-stack clinic platform: patients book appointments and share symptoms in
advance, doctors get an AI pre-visit summary, patients get an AI-simplified
post-visit summary, and both sides stay informed via email and Google Calendar.

**Stack:** Django + Django REST Framework + PostgreSQL (backend) · React + Vite
(frontend) · Anthropic Claude API (LLM) · Google Calendar API (OAuth2)

---

## 1. Project structure

```
backend/
  core/
    models.py         # Full DB schema
    permissions.py     # Role-based DRF permission classes
  api/
    serializers.py
    views.py
    urls.py
    services/
      booking_service.py     # Concurrency-safe slot hold/confirm logic
      llm_service.py         # Pre/post-visit AI summaries
      email_service.py       # Notification sending + retry log
      calendar_service.py    # Google Calendar OAuth2 + event sync
      reminder_service.py    # Medication reminder scheduling
    management/commands/
      run_scheduled_jobs.py  # Cron entry point for all background jobs
  requirements.txt
frontend/
  src/
    pages/  Login.jsx, PatientBooking.jsx, DoctorDashboard.jsx
    api/client.js            # Fetch wrapper with JWT + silent refresh
.env.example
SYSTEM_DESIGN.md             # 800-word design write-up (deliverable #4)
```

---

## 2. Setup guide

### Backend

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp ../.env.example .env   # fill in real values, see section 4 & 5 below

python manage.py migrate
python manage.py createsuperuser   # for Django Admin (doctor/leave management)
python manage.py runserver
```

Django Admin (`/admin/`) is where the admin portal's doctor-profile, working-hours,
and leave-day management happens directly — no custom admin UI was needed for
straightforward CRUD, per the assignment's admin scope.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env   # set VITE_API_BASE_URL
npm run dev
```

### Background jobs (medication reminders, hold expiry, retries)

Run periodically via cron (every 5–10 min recommended):

```bash
python manage.py run_scheduled_jobs
```

On Render: add this as a Cron Job resource pointing at the same repo/build. On
Railway: a scheduled deploy trigger. No Redis/Celery broker required — this was
a deliberate choice for free-tier deployment simplicity (see SYSTEM_DESIGN.md).

---

## 3. Database schema

See `backend/core/models.py` for the full annotated schema. Summary of entities:

| Model | Purpose |
|---|---|
| `User` / `PatientProfile` / `DoctorProfile` | Role-based identity |
| `DoctorWorkingHours` / `DoctorLeave` | Recurring availability + specific leave days |
| `Appointment` | Core booking entity; `UniqueConstraint(doctor, slot_start)` on active statuses prevents double-booking |
| `SymptomForm` → `PreVisitSummary` | Patient input → AI pre-visit output |
| `PostVisitNote` → `Prescription` → `PostVisitSummary` | Doctor input → AI patient-friendly output |
| `MedicationReminder` | Generated per-dose from `Prescription`, consumed by the background job |
| `NotificationLog` | Durable per-send record with retry_count, for safe email retries |
| `CalendarEvent` / `GoogleCalendarCredential` | Per-appointment Google Calendar event IDs + per-user OAuth tokens |

---

## 4. API overview

All endpoints under `/api/`. Auth via JWT (`Authorization: Bearer <token>`).

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/auth/register/patient/` | POST | Public | Patient self-registration |
| `/auth/login/` | POST | Public | Returns JWT (role embedded in claims) |
| `/admin/doctors/` | POST | Admin | Create doctor profile |
| `/admin/doctors/<id>/leave/` | POST | Admin | Mark leave date — cascades cancellation + notification |
| `/doctors/?specialization=` | GET | Any authenticated | Search doctors |
| `/doctors/<id>/slots/?date=` | GET | Any authenticated | Computed available slots |
| `/appointments/hold/` | POST | Patient | Step 1: reserve a slot (concurrency-safe) |
| `/appointments/<id>/symptoms/` | POST | Patient | Step 2: submit symptoms, confirms booking, triggers AI summary + email + calendar |
| `/appointments/<id>/pre-visit-summary/` | GET | Doctor | View AI pre-visit summary |
| `/appointments/<id>/post-visit-notes/` | POST | Doctor | Submit clinical notes + prescriptions, triggers AI patient summary |
| `/appointments/<id>/post-visit-summary/` | GET | Patient | View AI-simplified summary |
| `/appointments/mine/` | GET | Patient/Doctor | List own appointments |

---

## 5. LLM prompts (as specified)

**Pre-visit** (`llm_service.PRE_VISIT_PROMPT`):
> Analyse these symptoms and return urgency level (Low / Medium / High), chief
> complaint, and three suggested questions for the doctor.

**Post-visit** (`llm_service.POST_VISIT_PROMPT`):
> Convert these clinical notes into a patient-friendly summary with medication
> schedule and follow-up steps.

Both are sent with an explicit "return strict JSON only" instruction so responses
parse deterministically. Any failure (network, malformed JSON, unexpected schema)
is caught and recorded as `status='failed'` on the summary row — it never breaks
the booking or clinical flow. See SYSTEM_DESIGN.md for the full failure-handling
rationale.

---

## 6. Google Calendar setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project
   and enable the **Google Calendar API**.
2. Under **APIs & Services → Credentials**, create an **OAuth 2.0 Client ID**
   (type: Web application). Add your redirect URI (matches `GOOGLE_OAUTH_REDIRECT_URI`
   in `.env`).
3. Copy the client ID/secret into `.env`.
4. Each user (patient or doctor) connects their calendar by visiting the
   authorization URL generated by `calendar_service.get_authorization_url()`;
   the callback stores their access + refresh token in `GoogleCalendarCredential`.
5. Calendar sync is best-effort per user — if a user never connects, their side
   of the event is simply skipped (logged, not raised).

---

## 7. Deployment

Backend + Postgres deploy cleanly on Render or Railway free tiers. Frontend
deploys as a static Vite build on Vercel or Netlify, pointed at the backend's
public URL via `VITE_API_BASE_URL`.
