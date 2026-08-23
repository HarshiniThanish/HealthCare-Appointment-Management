# System Design Write-up

## Double-booking prevention

Double-booking is prevented at two layers, deliberately redundant.

**Application layer:** `booking_service.hold_slot()` runs inside a single
`transaction.atomic()` block. Before creating an `Appointment` row, it issues a
`SELECT ... FOR UPDATE` filtered to `(doctor, slot_start, status in [pending,
confirmed])`. If a matching row already exists, the request fails immediately
with a 409. If two requests for the same slot arrive concurrently, the second
transaction's `SELECT FOR UPDATE` **blocks** until the first transaction commits
or rolls back — it does not read stale data. Once unblocked, it re-checks and
correctly sees the slot as taken.

**Database layer:** a `UniqueConstraint` on `(doctor, slot_start)`, scoped by a
partial condition to `status in ['pending', 'confirmed']`, backstops the row
lock. This matters in a multi-instance deployment where two application servers
might not share a single Postgres connection's lock visibility in every edge
case — the constraint guarantees the database itself rejects a duplicate insert,
converted cleanly into a `SlotUnavailable` response via `IntegrityError` handling.
Scoping the constraint to active statuses (rather than all rows) means a
cancelled appointment doesn't permanently block that slot from being rebooked.

This two-layer approach means correctness doesn't depend on the row lock alone
being perfect — even a race the application layer somehow missed is caught by
the database's own uniqueness guarantee.

## Slot hold mechanism

Booking is a two-step flow, not a single atomic action, because the spec requires
a symptom form *before* a booking is confirmed. A naive implementation would let
a patient "reserve" a slot indefinitely by never finishing the form, silently
starving that slot forever.

The solution: `hold_slot()` creates the `Appointment` in a `pending` state with
`hold_expires_at = now + 10 minutes`. The slot is unavailable to other patients
immediately (the unique constraint covers `pending` too), but the hold is
soft-bounded. `confirm_booking()` — triggered by symptom-form submission — flips
the row to `confirmed` and clears the expiry. A background job,
`release_expired_holds()`, run every cycle via `run_scheduled_jobs`, finds
`pending` rows past their expiry and cancels them, freeing the slot for others.

This was chosen over a separate Redis-backed hold layer deliberately: it keeps
the entire booking guarantee inside a single source of truth (Postgres), avoids
a second infrastructure dependency on a free-tier deployment, and the 10-minute
window is generous enough that a legitimate patient filling out a symptom form
is never at risk of losing their slot mid-flow.

## Doctor leave conflict handling

When an admin marks a doctor on leave for a date that already has bookings, the
spec requires affected patients to be notified. `MarkDoctorLeaveView` handles
this as one atomic unit: inside a transaction, it creates the `DoctorLeave` row,
then locks (`select_for_update()`) and cancels every `pending`/`confirmed`
appointment on that doctor for that date, setting a clear
`cancellation_reason`. This guarantees the system can never be left in an
inconsistent state — a leave day recorded with a still-active booking against it.

Side effects (email notification, calendar event deletion) are deliberately run
**after** the transaction commits, not inside it. If the email provider is down,
that must not roll back a leave-day marking or un-cancel a patient's
appointment — the cancellation is the source of truth; the notification is a
best-effort follow-up, made durable through the retry mechanism below rather
than through transactional coupling.

The same `_slot_is_valid()` check that guards new bookings also re-validates
against `DoctorLeave` at hold time, closing the race where a leave day is added
between a patient viewing available slots and submitting a hold request.

## Notification failure handling

Every notification — booking confirmation, reminder, cancellation, leave
notice, medication reminder — creates a `NotificationLog` row *before* the send
is attempted, with `status='pending'`. The send itself is wrapped in a
try/except: success sets `status='sent'`; any exception (SMTP timeout, provider
rejection, invalid address) sets `status='failed'` and stores the error message,
without raising further. This means a failing email provider can never cause a
booking, cancellation, or clinical action to fail — notifications are always
decoupled from the operation that triggered them.

Failures aren't silently dropped, though: `retry_failed_notifications()`, run
every background-job cycle, finds `failed` rows under a `MAX_RETRIES` cap,
increments `retry_count`, and re-attempts the send. Capping retries prevents an
indefinitely bad address (e.g., a typo'd email) from being retried forever;
after the cap, the row remains visible in the log for manual follow-up rather
than disappearing.

The same never-raises pattern is applied to LLM calls (`llm_service`) and
calendar sync (`calendar_service`) for consistency — every external dependency
in this system degrades gracefully rather than cascading failure into the core
booking and clinical workflows.
