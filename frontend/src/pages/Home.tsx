import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { TopBar } from "../components/Chrome";
import { api, ApiError } from "../lib/api";
import type { TurnPayload } from "../lib/types";

// Consent comes first (spec §08): the session can't start until both boxes are ticked.
export default function Home() {
  const nav = useNavigate();
  const [ai, setAi] = useState(false);
  const [rec, setRec] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const res = await api<TurnPayload>("/conversations", {
        body: { consent_ai: ai, consent_recording: rec, language: "en-IN" },
      });
      nav(`/session/${res.conversation_id}`, { state: { first: res } });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start the session.");
      setBusy(false);
    }
  }

  return (
    <>
      <TopBar />
      <main className="centre-page">
        <section className="panel wide" aria-labelledby="welcome">
          <h1 id="welcome">Welcome to your skin consultation</h1>
          <p className="muted">
            An AI assistant will ask about your skin, take three photos and draft a care plan. A registered doctor reviews
            every plan before it's final, and can join at any time.
          </p>
          <div className="consent">
            <label>
              <input type="checkbox" checked={ai} onChange={(e) => setAi(e.target.checked)} />
              <span>I agree to talk with an AI assistant. I understand it is not a doctor.</span>
            </label>
            <label>
              <input type="checkbox" checked={rec} onChange={(e) => setRec(e.target.checked)} />
              <span>I agree that the conversation and my face photos are recorded in my clinic record.</span>
            </label>
          </div>
          {error && <p className="error-text" role="alert">{error}</p>}
          <div>
            <button className="btn" disabled={!ai || !rec || busy} onClick={start}>
              {busy ? "Starting…" : "Start consultation"}
            </button>
          </div>
        </section>
      </main>
    </>
  );
}
