import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, uid } from "../lib/api";
import type { ThreadMessage, ThreadSummary, ThreadView } from "../lib/types";

// The client's side of a follow-up chat, used on the clinic device (base = /conversations/:id, staff token) and on the
// client's phone (base = /me, client token). New messages are polled; nothing is kept on the device.
export default function ThreadChat({ base, threadId, token, onBack, backLabel = "‹ Back" }: {
  base: string;
  threadId: string;
  token?: string | null;
  onBack?: () => void;
  backLabel?: string;
}) {
  const [thread, setThread] = useState<ThreadSummary | null>(null);
  const [msgs, setMsgs] = useState<ThreadMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const opts = token !== undefined ? { token } : {};

  const merge = useCallback((incoming: ThreadMessage[]) => {
    if (!incoming.length) return;
    setMsgs((prev) => {
      const seen = new Set(prev.map((m) => m.id));
      return [...prev.filter((m) => !m.id.startsWith("local-")), ...incoming.filter((m) => !seen.has(m.id))]
        .sort((a, b) => a.seq - b.seq);
    });
  }, []);

  const lastSeq = msgs.filter((m) => !m.id.startsWith("local-")).reduce((mx, m) => Math.max(mx, m.seq), 0);

  useEffect(() => {
    setMsgs([]);
    setThread(null);
    api<ThreadView>(`${base}/threads/${threadId}`, opts).then((v) => { setThread(v.thread); merge(v.messages); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't open the chat."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, threadId, token, merge]);

  useEffect(() => {
    const t = setInterval(() => {
      if (busy) return;
      api<ThreadView>(`${base}/threads/${threadId}?after_seq=${lastSeq}`, opts)
        .then((v) => { setThread(v.thread); merge(v.messages); }).catch(() => undefined);
    }, 4000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, threadId, token, lastSeq, busy, merge]);

  useEffect(() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight }); }, [msgs.length, busy]);

  async function send(e: FormEvent) {
    e.preventDefault();
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    setText("");
    const cmid = uid();
    const local: ThreadMessage = { id: `local-${cmid}`, seq: Number.MAX_SAFE_INTEGER, role: "client", content: value,
      author: null, author_role: null, channel: null, created_at: "" };
    setMsgs((prev) => [...prev, local]);
    try {
      let out: ThreadView | null = null;
      for (let attempt = 0; attempt < 2 && !out; attempt++) {
        try {
          out = await api<ThreadView>(`${base}/threads/${threadId}/messages`,
            { ...opts, body: { text: value, client_message_id: cmid } });
        } catch (err) {
          if (attempt === 1 || (err instanceof ApiError && err.status < 500)) throw err;
        }
      }
      setThread(out!.thread);
      merge(out!.messages);
    } catch (err) {
      setMsgs((prev) => prev.filter((m) => m.id !== local.id));
      setText(value);
      setError(err instanceof ApiError ? err.message : "Message not sent. Check the connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  const withTeam = thread?.mode === "staff";
  return (
    <div className="thread-chat">
      <div className="thread-head">
        {onBack && <button className="btn quiet small" onClick={onBack}>{backLabel}</button>}
        <div>
          <strong>{thread?.title ?? "Chat"}</strong>
          {thread?.date && <span className="tiny"> · {new Date(thread.date).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}</span>}
        </div>
      </div>
      <div ref={listRef} className="transcript" aria-live="polite">
        {!msgs.length && thread && (
          <p className="msg system">
            {thread.visit_id
              ? "Ask anything about this consultation: your plan, how to use it, your appointments. The assistant answers from your doctor-approved plan, and you can ask for the clinic team any time."
              : "Write to the clinic. The assistant answers from your doctor-approved plans, and you can ask for the clinic team any time."}
          </p>
        )}
        {msgs.map((m) => (
          <div key={m.id} className={`msg ${m.role === "client" ? "user" : m.role === "staff" ? "doctor" : m.role}`}>
            {m.role === "staff" && <div className="who-line">{m.author}{m.author_role === "clinic team" ? " (clinic team)" : ""}</div>}
            {m.role === "assistant" && <div className="who-line">AI assistant</div>}
            {m.role === "system" ? m.content : <div className="bubble">{m.content}</div>}
          </div>
        ))}
        {busy && <div className="typing">…</div>}
      </div>
      <div className="composer">
        {withTeam && (
          <p className="tiny" role="status">
            {thread?.waiting ? "Your message is with the clinic team. They'll reply here, and we'll text you when they do."
              : "You're chatting with the clinic team."}
          </p>
        )}
        {error && <p className="error-text" role="alert">{error}</p>}
        <form className="send-row" onSubmit={send}>
          <label className="sr-only" htmlFor={`say-${threadId}`} style={{ position: "absolute", left: -9999 }}>Your message</label>
          <input id={`say-${threadId}`} type="text" value={text} onChange={(e) => setText(e.target.value)} disabled={busy || !thread}
            maxLength={2000} placeholder={withTeam ? "Message the clinic team" : "Ask about your plan"} />
          <button className="btn" disabled={busy || !text.trim()}>Send</button>
        </form>
      </div>
    </div>
  );
}
