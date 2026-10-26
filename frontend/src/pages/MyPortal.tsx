import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import HistoryPanel from "../components/HistoryPanel";
import { api, ApiError } from "../lib/api";
import { clientImageSrc, Letter } from "./Summary";

// A client on their own phone: sign in with their mobile number and a one-time code, then read their consultations
// and chat with the clinic. Its token is kept apart from staff sign-ins and only for this browser tab (NFR-06).
const KEY = "skin.client";

function readToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function saveToken(t: string | null) {
  try {
    if (t) sessionStorage.setItem(KEY, t);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: this tab keeps it in memory only */
  }
}

interface Me { first_name: string; full_name: string; mobile: string; unread: number }

function useClient() {
  const [token, setTokenState] = useState<string | null>(readToken);
  const [me, setMe] = useState<Me | null>(null);
  const signOut = useCallback(() => { saveToken(null); setTokenState(null); setMe(null); }, []);
  const signIn = useCallback((t: string) => { saveToken(t); setTokenState(t); }, []);
  useEffect(() => {
    if (!token) return;
    api<Me>("/me", { token }).then(setMe).catch(() => signOut());
  }, [token, signOut]);
  useEffect(() => {
    window.addEventListener("client:expired", signOut);
    return () => window.removeEventListener("client:expired", signOut);
  }, [signOut]);
  return { token, me, signIn, signOut };
}

function Frame({ me, onSignOut, children }: { me: Me | null; onSignOut?: () => void; children: ReactNode }) {
  return (
    <>
      <header className="topbar">
        <Link to="/my" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
          <span className="brand-mark" aria-hidden /> Your skin care
        </Link>
        <span className="spacer" />
        {me && <span className="who">{me.full_name}</span>}
        {onSignOut && <button className="btn quiet small" onClick={onSignOut}>Sign out</button>}
      </header>
      {children}
    </>
  );
}

function SignIn({ onSignedIn }: { onSignedIn: (token: string) => void }) {
  const [mobile, setMobile] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<{ mock: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setSent(await api("/client-auth/start", { body: { mobile }, auth: false }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the clinic right now. Try again shortly.");
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ access_token: string }>("/client-auth/verify", { body: { mobile, code }, auth: false });
      onSignedIn(r.access_token);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the clinic right now. Try again shortly.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="centre-page">
      {!sent ? (
        <form className="panel" onSubmit={send}>
          <h1>Your consultations</h1>
          <p className="muted">Sign in with the mobile number you gave the clinic to see your plans and chat with us.</p>
          <div className="field">
            <label htmlFor="m">Mobile number</label>
            <input id="m" type="tel" inputMode="numeric" autoComplete="tel-national" placeholder="10-digit mobile number"
              value={mobile} onChange={(e) => setMobile(e.target.value)} required />
          </div>
          {error && <p className="error-text" role="alert">{error}</p>}
          <button className="btn" disabled={busy || mobile.replace(/\D/g, "").length < 10}>{busy ? "Sending…" : "Send me a code"}</button>
        </form>
      ) : (
        <form className="panel" onSubmit={verify}>
          <h1>Enter your code</h1>
          <p className="muted">{sent.message}</p>
          <div className="field">
            <label htmlFor="c">6-digit code</label>
            <input id="c" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} required />
          </div>
          {sent.mock && <p className="demo-hint">Demo mode: the code is <b>123456</b>.</p>}
          {error && <p className="error-text" role="alert">{error}</p>}
          <button className="btn" disabled={busy || code.length !== 6}>{busy ? "Checking…" : "Sign in"}</button>
          <div className="actions-bar">
            <button type="button" className="linkish" onClick={() => send()} disabled={busy}>Send a new code</button>
            <button type="button" className="linkish" onClick={() => { setSent(null); setCode(""); setError(null); }}>Use another number</button>
          </div>
        </form>
      )}
    </main>
  );
}

export function MyPortal() {
  const { token, me, signIn, signOut } = useClient();
  if (!token) return <Frame me={null}><SignIn onSignedIn={signIn} /></Frame>;
  return (
    <Frame me={me} onSignOut={signOut}>
      <main className="my-page">
        <div className="my-head">
          <h1>{me ? `Hello, ${me.first_name}` : "Hello"}</h1>
          <p className="muted">Your consultations, upcoming treatments and messages with the clinic.</p>
        </div>
        <div className="my-body">
          <HistoryPanel base="/me" token={token} summaryHref={(v) => `/my/visits/${v}`} />
        </div>
      </main>
    </Frame>
  );
}

export function MyVisitSummary() {
  const { visitId } = useParams<{ visitId: string }>();
  const { token, me, signIn, signOut } = useClient();
  const [s, setS] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!token) return;
    api(`/me/visits/${visitId}/summary`, { token }).then(setS)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load that summary."));
  }, [token, visitId]);
  if (!token) return <Frame me={null}><SignIn onSignedIn={signIn} /></Frame>;
  return (
    <Frame me={me} onSignOut={signOut}>
      <main className="summary-page">
        <div className="no-print summary-back"><Link className="btn quiet small" to="/my">‹ Back to your consultations</Link></div>
        {s ? <Letter s={s} imageSrc={clientImageSrc(token)} /> : <p className={error ? "error-text" : "muted"}>{error ?? "Loading…"}</p>}
      </main>
    </Frame>
  );
}
