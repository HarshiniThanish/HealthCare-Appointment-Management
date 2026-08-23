import { useState, useEffect } from "react";
import { api } from "../api/client";

const URGENCY_CLASS = { Low: "urgency-low", Medium: "urgency-medium", High: "urgency-high" };

const EMPTY_PRESCRIPTION = {
  medication_name: "", dosage: "", times_per_day: 1, duration_days: 5,
  start_date: new Date().toISOString().slice(0, 10), notes: "",
};

export default function DoctorDashboard() {
  const [appointments, setAppointments] = useState([]);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [preVisit, setPreVisit] = useState(null);
  const [preVisitLoading, setPreVisitLoading] = useState(false);

  const [clinicalNotes, setClinicalNotes] = useState("");
  const [prescriptions, setPrescriptions] = useState([{ ...EMPTY_PRESCRIPTION }]);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => { loadAppointments(); }, []);

  async function loadAppointments() {
    setError("");
    try {
      setAppointments(await api.myAppointments());
    } catch (err) {
      setError(err.message);
    }
  }

  async function selectAppointment(appt) {
    setSelectedId(appt.id);
    setPreVisit(null);
    setSubmitted(false);
    setClinicalNotes("");
    setPrescriptions([{ ...EMPTY_PRESCRIPTION }]);
    setPreVisitLoading(true);
    try {
      setPreVisit(await api.getPreVisitSummary(appt.id));
    } catch {
      setPreVisit(null); // no summary yet, or generation failed — form still usable
    } finally {
      setPreVisitLoading(false);
    }
  }

  function updatePrescription(index, field, value) {
    setPrescriptions((prev) => prev.map((p, i) => (i === index ? { ...p, [field]: value } : p)));
  }

  async function handleSubmitNote() {
    setError("");
    setSubmitting(true);
    try {
      await api.submitPostVisitNote(selectedId, {
        clinical_notes: clinicalNotes,
        prescriptions: prescriptions.filter((p) => p.medication_name.trim()),
      });
      setSubmitted(true);
      loadAppointments();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  const selected = appointments.find((a) => a.id === selectedId);

  return (
    <div style={{ display: "flex", gap: 28 }}>
      <div style={{ flex: "0 0 320px" }}>
        <h1 style={{ fontSize: "1.4rem" }}>Today's schedule</h1>
        {error && <div className="error-banner">{error}</div>}
        <div className="card">
          {appointments.length === 0 ? (
            <div className="empty-state">No appointments yet.</div>
          ) : appointments.map((appt) => (
            <div key={appt.id} className="appt-row" onClick={() => selectAppointment(appt)}
                 style={{ background: selectedId === appt.id ? "var(--paper)" : "transparent" }}>
              <div>
                <div>{appt.patient_name}</div>
                <div className="appt-time">{new Date(appt.slot_start).toLocaleString()}</div>
              </div>
              <span className={`urgency-badge urgency-pending`} style={{ fontSize: "0.65rem" }}>
                {appt.status}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div style={{ flex: 1 }}>
        {!selected && <div className="empty-state">Select an appointment to view details.</div>}

        {selected && (
          <>
            <h2>{selected.patient_name}</h2>
            <p className="appt-time" style={{ marginTop: -8, marginBottom: 18 }}>
              {new Date(selected.slot_start).toLocaleString()}
            </p>

            <div className="card">
              <h3>Pre-visit AI summary</h3>
              {preVisitLoading && <div className="empty-state">Loading…</div>}
              {!preVisitLoading && !preVisit && (
                <div className="empty-state">No summary available yet.</div>
              )}
              {!preVisitLoading && preVisit && preVisit.status === "failed" && (
                <div className="error-banner">AI summary generation failed. Review symptoms manually.</div>
              )}
              {!preVisitLoading && preVisit && preVisit.status === "success" && (
                <div>
                  <span className={`urgency-badge ${URGENCY_CLASS[preVisit.urgency_level] || ""}`}>
                    {preVisit.urgency_level} urgency
                  </span>
                  <p style={{ marginTop: 12 }}>{preVisit.chief_complaint}</p>
                  <div style={{ fontSize: "0.85rem", color: "var(--slate)" }}>Suggested questions:</div>
                  <ul style={{ marginTop: 4, fontSize: "0.9rem" }}>
                    {preVisit.suggested_questions.map((q, i) => <li key={i}>{q}</li>)}
                  </ul>
                </div>
              )}
            </div>

            {selected.status === "completed" || submitted ? (
              <div className="card">
                <h3>Post-visit note submitted</h3>
                <p style={{ color: "var(--slate)", fontSize: "0.85rem" }}>
                  The patient-friendly summary and medication reminders have been generated.
                </p>
              </div>
            ) : (
              <div className="card">
                <h3>Post-visit note</h3>
                <div className="field">
                  <label>Clinical notes</label>
                  <textarea
                    rows={4} value={clinicalNotes}
                    onChange={(e) => setClinicalNotes(e.target.value)}
                    placeholder="Diagnosis, observations, treatment plan…"
                  />
                </div>

                <label>Prescriptions</label>
                {prescriptions.map((p, i) => (
                  <div key={i} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <input placeholder="Medication" value={p.medication_name}
                           onChange={(e) => updatePrescription(i, "medication_name", e.target.value)} />
                    <input placeholder="Dosage" style={{ maxWidth: 100 }} value={p.dosage}
                           onChange={(e) => updatePrescription(i, "dosage", e.target.value)} />
                    <input type="number" min={1} max={6} style={{ maxWidth: 70 }} value={p.times_per_day}
                           onChange={(e) => updatePrescription(i, "times_per_day", Number(e.target.value))} />
                    <input type="number" min={1} style={{ maxWidth: 80 }} value={p.duration_days}
                           onChange={(e) => updatePrescription(i, "duration_days", Number(e.target.value))} />
                  </div>
                ))}
                <button className="btn btn-ghost" style={{ marginBottom: 16 }}
                        onClick={() => setPrescriptions((prev) => [...prev, { ...EMPTY_PRESCRIPTION }])}>
                  + Add medication
                </button>

                <div>
                  <button className="btn btn-primary" disabled={!clinicalNotes.trim() || submitting}
                          onClick={handleSubmitNote}>
                    {submitting ? "Submitting…" : "Submit and generate patient summary"}
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
