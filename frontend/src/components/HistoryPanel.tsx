import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { ThreadSummary } from "../lib/types";
import { formatDate } from "./Chrome";
import ThreadChat from "./ThreadChat";

interface PastVisit {
  visit_id: string;
  date: string;
  programme: string;
  session_number: number;
  concern: string | null;
  status: string;
  approved: boolean;
  findings: { label: string; severity: string; score: number }[];
  plan: { name: string; kind: string; sessions: number | null }[];
  next_session_date: string | null;
  appointments: { procedure: string; status: string; label: string | null; room_name: string | null }[];
}

interface History {
  visits: PastVisit[];
  upcoming: { procedure: string; label: string; room_name: string; session_index: number; sessions_total: number; tentative?: boolean }[];
}

function ago(iso: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 24 * 60) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

// The client's own past consultations and their chats with the clinic. Only approved plans and findings are shown
// (a doctor signed them off). `base` is /conversations/:id on the clinic device or /me on the client's phone.
export default function HistoryPanel({ base, token, summaryHref, onUnreadChange }: {
  base: string;
  token?: string | null;
  summaryHref: (visitId: string) => string;
  onUnreadChange?: () => void;
}) {
  const [h, setH] = useState<History | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [allUpcoming, setAllUpcoming] = useState(false);
  const [chat, setChat] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const opts = token !== undefined ? { token } : {};

  const loadThreads = useCallback(() => {
    api<{ threads: ThreadSummary[] }>(`${base}/threads`, opts).then((r) => setThreads(r.threads)).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, token]);

  useEffect(() => {
    api<History>(`${base}/history`, opts).then(setH)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load your history."));
    loadThreads();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, token, loadThreads]);

  useEffect(() => {
    if (chat) return;
    const t = setInterval(loadThreads, 10000);
    return () => clearInterval(t);
  }, [chat, loadThreads]);

  async function open(visitId: string | null) {
    setOpening(true);
    setError(null);
    try {
      const t = await api<ThreadSummary>(`${base}/threads`, { ...opts, body: { visit_id: visitId } });
      setChat(t.id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't open the chat.");
    } finally {
      setOpening(false);
    }
  }

  if (chat) {
    return <ThreadChat base={base} threadId={chat} token={token}
      onBack={() => { setChat(null); loadThreads(); onUnreadChange?.(); }} backLabel="‹ All consultations" />;
  }
  if (!h) return <div className="results"><p className={error ? "error-text" : "muted"}>{error ?? "Loading your history…"}</p></div>;
  const byVisit = new Map(threads.filter((t) => t.visit_id).map((t) => [t.visit_id!, t]));
  const general = threads.find((t) => !t.visit_id);
  return (
    <div className="results history-panel">
      {error && <p className="error-text" role="alert">{error}</p>}
      <section className="section" aria-labelledby="msg-h">
        <div className="section-head">
          <h3 id="msg-h">Messages</h3>
          {!general && <button className="btn quiet small" onClick={() => open(null)} disabled={opening}>Write to the clinic</button>}
        </div>
        {!threads.length && <p className="muted">No messages yet. Ask about any consultation below, or write to the clinic.</p>}
        <ul className="thread-list">
          {threads.map((t) => (
            <li key={t.id}>
              <button className={`thread-row${t.unread ? " unread" : ""}`} onClick={() => setChat(t.id)}>
                <span className="t-top">
                  <strong>{t.title}</strong>
                  <span className="tiny">{ago(t.last_message_at)}</span>
                </span>
                {t.last_message && (
                  <span className="t-last">
                    {t.last_message.role === "client" ? "You: " : t.last_message.role === "staff" ? `${t.last_message.author}: ` : ""}
                    {t.last_message.content}
                  </span>
                )}
                <span className="t-tags">
                  {t.unread > 0 && <span className="pill new">{t.unread} new</span>}
                  {t.waiting && <span className="pill">With the clinic team</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {h.upcoming.length > 0 && (
        <section className="section" aria-labelledby="up-h">
          <h3 id="up-h">Upcoming treatments</h3>
          <ul className="appts">
            {(allUpcoming ? h.upcoming : h.upcoming.slice(0, 3)).map((u, i) => (
              <li key={i} className={u.tentative ? "tentative" : "confirmed"}>
                <span className="when">{u.label}{u.tentative && <span className="tiny"> · to be confirmed</span>}</span>
                <span className="what"><strong>{u.procedure}</strong>{u.sessions_total > 1 ? `, session ${u.session_index} of ${u.sessions_total}` : ""}</span>
                <span className="where">{u.room_name}</span>
              </li>
            ))}
          </ul>
          {h.upcoming.length > 3 && (
            <button className="linkish" onClick={() => setAllUpcoming(!allUpcoming)}>
              {allUpcoming ? "Show fewer" : `Show all ${h.upcoming.length} upcoming treatments`}
            </button>
          )}
        </section>
      )}

      <section className="section" aria-labelledby="past-h">
        <h3 id="past-h">Past consultations</h3>
        {!h.visits.length && <p className="muted">No earlier consultations.</p>}
        {h.visits.map((v) => {
          const t = byVisit.get(v.visit_id);
          return (
            <article key={v.visit_id} className="past-visit">
              <header>
                <div>
                  <strong>{formatDate(v.date)}</strong>
                  <span className="muted"> · {v.programme}, session {v.session_number}</span>
                </div>
                <span className={v.approved ? "approved-stamp" : "draft-stamp"}>{v.status}</span>
              </header>
              {v.concern && <p className="tiny">You said: “{v.concern}”</p>}
              {v.findings.length > 0 && (
                <p>Found: {v.findings.map((f) => `${f.severity} ${f.label}`).join(", ")}.</p>
              )}
              {v.plan.length > 0 && (
                <p>Plan: {v.plan.map((p) => p.name + (p.sessions && p.sessions > 1 ? ` (${p.sessions} sessions)` : "")).join(", ")}.</p>
              )}
              {!v.approved && <p className="tiny">This visit has no doctor-approved plan, so its details aren't shown here.</p>}
              <div className="actions-bar">
                <button className="btn small" onClick={() => (t ? setChat(t.id) : open(v.visit_id))} disabled={opening}>
                  {t ? "Open chat" : "Ask about this visit"}{t && t.unread > 0 ? ` (${t.unread} new)` : ""}
                </button>
                {v.approved && <Link className="btn quiet small" to={summaryHref(v.visit_id)}>View the full summary</Link>}
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
