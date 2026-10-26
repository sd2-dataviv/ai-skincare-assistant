import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const u = await login(email, password);
      nav(u.role === "operator" ? "/" : "/doctor", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server. Check that the API is running.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="centre-page">
      <form className="panel" onSubmit={submit}>
        <div className="brand"><span className="brand-mark" aria-hidden /> Skin consultation</div>
        <h1>Sign in</h1>
        <p className="muted">Use your WorkflowHub email and password, or your clinic account.</p>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </div>
        {error && <p className="error-text" role="alert">{error}</p>}
        <button className="btn" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        {import.meta.env.DEV && (
          <p className="demo-hint">
            Demo accounts (password <b>Demo@12345</b>): operator@clinic.local for the clinic device, doctor@clinic.local
            for doctor review.
          </p>
        )}
      </form>
    </main>
  );
}
