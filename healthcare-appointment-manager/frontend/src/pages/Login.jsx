import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";

function decodeRole(accessToken) {
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return { role: payload.role, name: payload.name };
  } catch {
    return { role: null, name: null };
  }
}

export default function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const data = await api.login(username, password);
      const { role, name } = decodeRole(data.access);
      onLogin({ role, name });
      navigate(role === "doctor" ? "/doctor" : "/book");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ maxWidth: 360, margin: "80px auto", padding: "0 20px" }}>
      <h1 style={{ marginBottom: 4 }}>Clinic</h1>
      <p style={{ color: "var(--slate)", marginBottom: 28, fontSize: "0.9rem" }}>
        Sign in to manage appointments.
      </p>

      {error && <div className="error-banner">{error}</div>}

      <form onSubmit={handleSubmit} className="card">
        <div className="field">
          <label htmlFor="username">Username</label>
          <input id="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} required
          />
        </div>
        <button className="btn btn-primary" type="submit" disabled={loading} style={{ width: "100%" }}>
          {loading ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
