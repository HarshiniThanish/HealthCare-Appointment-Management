import { useState, useEffect } from "react";
import { api } from "../api/client";

const STEPS = ["Find a doctor", "Pick a slot", "Symptoms", "Confirmed"];

export default function PatientBooking() {
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");

  const [specialization, setSpecialization] = useState("");
  const [doctors, setDoctors] = useState([]);
  const [selectedDoctor, setSelectedDoctor] = useState(null);

  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [slots, setSlots] = useState([]);
  const [selectedSlot, setSelectedSlot] = useState(null);
  const [appointment, setAppointment] = useState(null);

  const [symptomsText, setSymptomsText] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => { searchDoctors(); }, []);

  async function searchDoctors() {
    setError("");
    try {
      setDoctors(await api.searchDoctors(specialization));
    } catch (err) {
      setError(err.message);
    }
  }

  async function pickDoctor(doctor) {
    setSelectedDoctor(doctor);
    setStep(1);
    await loadSlots(doctor.id, date);
  }

  async function loadSlots(doctorId, forDate) {
    setError("");
    setSelectedSlot(null);
    try {
      const data = await api.getAvailableSlots(doctorId, forDate);
      setSlots(data.available_slots);
    } catch (err) {
      setError(err.message);
    }
  }

  async function holdSlot() {
    setError("");
    try {
      const appt = await api.holdSlot(selectedDoctor.id, selectedSlot);
      setAppointment(appt);
      setStep(2);
    } catch (err) {
      // 409 means someone else took it in the meantime — refresh the slot list.
      setError(err.message);
      loadSlots(selectedDoctor.id, date);
    }
  }

  async function submitSymptoms() {
    setError("");
    setSubmitting(true);
    try {
      const confirmed = await api.submitSymptoms(appointment.id, symptomsText);
      setAppointment(confirmed);
      setStep(3);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <h1>Book an appointment</h1>
      <div style={{ display: "flex", gap: 6, marginBottom: 24 }}>
        {STEPS.map((label, i) => (
          <span key={label} style={{
            fontFamily: "var(--font-mono)", fontSize: "0.72rem", padding: "4px 10px",
            borderRadius: 100, background: i === step ? "var(--teal)" : "var(--slate-light)",
            color: i === step ? "white" : "var(--slate)",
          }}>{i + 1}. {label}</span>
        ))}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {step === 0 && (
        <div>
          <div className="card" style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Specialization</label>
              <input
                placeholder="e.g. Cardiology"
                value={specialization}
                onChange={(e) => setSpecialization(e.target.value)}
              />
            </div>
            <button className="btn btn-primary" onClick={searchDoctors}>Search</button>
          </div>

          {doctors.length === 0 ? (
            <div className="empty-state">No doctors found. Try a different specialization.</div>
          ) : doctors.map((doc) => (
            <div key={doc.id} className="card" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <h3 style={{ marginBottom: 2 }}>{doc.name || doc.username}</h3>
                <div style={{ color: "var(--slate)", fontSize: "0.85rem" }}>{doc.specialization}</div>
              </div>
              <button className="btn btn-ghost" onClick={() => pickDoctor(doc)}>Select</button>
            </div>
          ))}
        </div>
      )}

      {step === 1 && selectedDoctor && (
        <div className="card">
          <h3>{selectedDoctor.name || selectedDoctor.username} — {selectedDoctor.specialization}</h3>
          <div className="field" style={{ maxWidth: 220 }}>
            <label>Date</label>
            <input
              type="date" value={date}
              onChange={(e) => { setDate(e.target.value); loadSlots(selectedDoctor.id, e.target.value); }}
            />
          </div>

          {slots.length === 0 ? (
            <div className="empty-state">No open slots on this date. Try another day.</div>
          ) : (
            <div className="slot-grid">
              {slots.map((s) => {
                const label = new Date(s).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
                return (
                  <div
                    key={s}
                    className={`slot-chip ${selectedSlot === s ? "selected" : ""}`}
                    onClick={() => setSelectedSlot(s)}
                  >{label}</div>
                );
              })}
            </div>
          )}

          <button className="btn btn-primary" disabled={!selectedSlot} onClick={holdSlot}>
            Hold this slot
          </button>
        </div>
      )}

      {step === 2 && appointment && (
        <div className="card">
          <h3>Describe your symptoms</h3>
          <p style={{ color: "var(--slate)", fontSize: "0.85rem", marginTop: -8 }}>
            This helps your doctor prepare — an AI-generated summary and urgency
            level will be shared with them before the visit.
          </p>
          <div className="field">
            <textarea
              rows={5} value={symptomsText}
              onChange={(e) => setSymptomsText(e.target.value)}
              placeholder="e.g. Persistent headache for 3 days, mild fever, sensitivity to light…"
            />
          </div>
          <button
            className="btn btn-primary" disabled={!symptomsText.trim() || submitting}
            onClick={submitSymptoms}
          >
            {submitting ? "Confirming…" : "Confirm booking"}
          </button>
        </div>
      )}

      {step === 3 && appointment && (
        <div className="card">
          <h3>You're booked</h3>
          <p style={{ fontFamily: "var(--font-mono)", fontSize: "0.9rem" }}>
            {new Date(appointment.slot_start).toLocaleString()}
          </p>
          <p style={{ color: "var(--slate)", fontSize: "0.85rem" }}>
            A confirmation email and calendar invite are on their way. You can view
            your post-visit summary here once the appointment is complete.
          </p>
        </div>
      )}
    </div>
  );
}
