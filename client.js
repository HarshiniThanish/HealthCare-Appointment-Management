const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api";

function getTokens() {
  return {
    access: localStorage.getItem("access_token"),
    refresh: localStorage.getItem("refresh_token"),
  };
}

function setTokens({ access, refresh }) {
  if (access) localStorage.setItem("access_token", access);
  if (refresh) localStorage.setItem("refresh_token", refresh);
}

export function clearTokens() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
}

async function request(path, { method = "GET", body, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const { access } = getTokens();
    if (access) headers["Authorization"] = `Bearer ${access}`;
  }

  let res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  // One silent retry on 401 using the refresh token, so a short-lived access
  // token doesn't force the user to re-login mid-session.
  if (res.status === 401 && auth) {
    const { refresh } = getTokens();
    if (refresh) {
      const refreshRes = await fetch(`${BASE_URL}/auth/refresh/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh }),
      });
      if (refreshRes.ok) {
        const data = await refreshRes.json();
        setTokens({ access: data.access });
        headers["Authorization"] = `Bearer ${data.access}`;
        res = await fetch(`${BASE_URL}${path}`, {
          method,
          headers,
          body: body ? JSON.stringify(body) : undefined,
        });
      } else {
        clearTokens();
      }
    }
  }

  const isJson = res.headers.get("content-type")?.includes("application/json");
  const data = isJson ? await res.json() : null;

  if (!res.ok) {
    throw new Error(data?.detail || `Request failed (${res.status})`);
  }
  return data;
}

export const api = {
  login: (username, password) =>
    request("/auth/login/", { method: "POST", body: { username, password }, auth: false })
      .then((data) => { setTokens(data); return data; }),

  registerPatient: (payload) =>
    request("/auth/register/patient/", { method: "POST", body: payload, auth: false }),

  searchDoctors: (specialization = "") =>
    request(`/doctors/${specialization ? `?specialization=${encodeURIComponent(specialization)}` : ""}`),

  getAvailableSlots: (doctorId, date) =>
    request(`/doctors/${doctorId}/slots/?date=${date}`),

  holdSlot: (doctorId, slotStart) =>
    request("/appointments/hold/", { method: "POST", body: { doctor_id: doctorId, slot_start: slotStart } }),

  submitSymptoms: (appointmentId, symptomsText) =>
    request(`/appointments/${appointmentId}/symptoms/`, {
      method: "POST", body: { symptoms_text: symptomsText },
    }),

  myAppointments: () => request("/appointments/mine/"),

  getPreVisitSummary: (appointmentId) =>
    request(`/appointments/${appointmentId}/pre-visit-summary/`),

  submitPostVisitNote: (appointmentId, payload) =>
    request(`/appointments/${appointmentId}/post-visit-notes/`, { method: "POST", body: payload }),
};

export { getTokens };
