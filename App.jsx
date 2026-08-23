import { useState } from "react";
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from "react-router-dom";
import Login from "./pages/Login";
import PatientBooking from "./pages/PatientBooking";
import DoctorDashboard from "./pages/DoctorDashboard";
import { clearTokens } from "./api/client";

function Shell({ user, onLogout, children }) {
  return (
    <div className="app-shell">
      <aside className="rail">
        <div className="rail-brand">Clinic</div>
        <div className="rail-role">{user.role}</div>
        <div style={{ fontWeight: 500, marginBottom: 24 }}>{user.name}</div>
        <button className="btn btn-ghost" style={{ width: "100%" }} onClick={onLogout}>
          Sign out
        </button>
      </aside>
      <main className="content">{children}</main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(null);

  return (
    <BrowserRouter>
      <AppRoutes user={user} setUser={setUser} />
    </BrowserRouter>
  );
}

function AppRoutes({ user, setUser }) {
  const navigate = useNavigate();

  function handleLogout() {
    clearTokens();
    setUser(null);
    navigate("/login");
  }

  return (
    <Routes>
      <Route path="/login" element={<Login onLogin={setUser} />} />

      <Route path="/book" element={
        user ? <Shell user={user} onLogout={handleLogout}><PatientBooking /></Shell>
             : <Navigate to="/login" replace />
      } />

      <Route path="/doctor" element={
        user ? <Shell user={user} onLogout={handleLogout}><DoctorDashboard /></Shell>
             : <Navigate to="/login" replace />
      } />

      <Route path="*" element={<Navigate to={user ? (user.role === "doctor" ? "/doctor" : "/book") : "/login"} replace />} />
    </Routes>
  );
}
