import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { StageRail, TopBar } from "../components/Chrome";
import { Mirror, type MirrorHandle } from "../components/Mirror";
import HistoryPanel from "../components/HistoryPanel";
import ResultsPanel from "../components/ResultsPanel";
import { api, ApiError, uid } from "../lib/api";
import { speechInputSupported, speechOutputSupported, useSpeechInput, useSpeechOutput } from "../lib/speech";
import type { Message, TurnPayload, UiHints } from "../lib/types";

const POLL_STAGES = new Set(["approval", "handed_over", "analyse", "recommend"]);

export default function Session() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  const nav = useNavigate();
  const first = (location.state as { first?: TurnPayload } | null)?.first;

  const [messages, setMessages] = useState<Message[]>(first?.messages ?? []);
  const [ui, setUi] = useState<UiHints | null>(first?.ui ?? null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"talk" | "results" | "history">("talk");
  const [voiceOn, setVoiceOn] = useState(speechOutputSupported());
  const [resultsVersion, setResultsVersion] = useState(0);
  const mirrorRef = useRef<MirrorHandle>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const spokenRef = useRef<Set<string>>(new Set((first?.messages ?? []).map((m) => m.id)));

  const { speaking, speak, cancel } = useSpeechOutput("en-IN");

  const absorb = useCallback((payload: TurnPayload) => {
    setUi(payload.ui);
    if (payload.messages.length) {
      setMessages((prev) => {
        const seen = new Set(prev.map((m) => m.id));
        return [...prev.filter((m) => !m.id.startsWith("local-")), ...payload.messages.filter((m) => !seen.has(m.id))]
          .sort((a, b) => a.seq - b.seq);
      });
      setResultsVersion((v) => v + 1);
    }
  }, []);

  // Speak new assistant/doctor lines (voice can be turned off).
  useEffect(() => {
    const fresh = messages.filter((m) => (m.role === "assistant" || m.role === "doctor") && !spokenRef.current.has(m.id));
    fresh.forEach((m) => spokenRef.current.add(m.id));
    if (voiceOn && fresh.length) fresh.forEach((m) => speak(m.content));
  }, [messages, voiceOn, speak]);

  useEffect(() => {
    if (first && voiceOn) first.messages.forEach((m) => speak(m.content));
    // The greeting handed over by the start page is used once. Clear it from this history entry so that coming
    // back here (e.g. Back from the summary) reloads the real conversation instead of replaying the greeting.
    if (first) nav(location.pathname, { replace: true, state: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Initial load (e.g. after a refresh) — the transcript is re-read from the server, never from the device.
  useEffect(() => {
    if (first || !id) return;
    api<TurnPayload>(`/conversations/${id}`).then((p) => {
      p.messages.forEach((m) => spokenRef.current.add(m.id));
      absorb(p);
    }).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the session."));
  }, [id, first, absorb]);

  // While a doctor is reviewing or has taken over, poll for their messages and decisions.
  const lastSeq = messages.filter((m) => !m.id.startsWith("local-")).reduce((mx, m) => Math.max(mx, m.seq), 0);
  // Once the client is known, check now and then for messages from the clinic (shown as a badge on History).
  const shouldPoll = !!ui && (POLL_STAGES.has(ui.stage) || ui.mode === "handed_over");
  const pollMs = shouldPoll ? 3000 : ui?.client_first_name ? 10000 : 0;
  const refresh = useCallback(() => {
    if (!id) return;
    api<TurnPayload>(`/conversations/${id}?after_seq=${lastSeq}`).then(absorb).catch(() => undefined);
  }, [id, lastSeq, absorb]);
  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(refresh, pollMs);
    return () => clearInterval(t);
  }, [pollMs, refresh]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages, busy, tab]);

  const send = useCallback(async (raw: string) => {
    const value = raw.trim();
    if (!value || !id || busy) return;
    cancel();
    setBusy(true);
    setError(null);
    setText("");
    const cmid = uid();
    const local: Message = { id: `local-${cmid}`, seq: Number.MAX_SAFE_INTEGER, role: "user", content: value, meta: {}, created_at: "" };
    setMessages((prev) => [...prev, local]);
    try {
      // Re-sent with the same client_message_id on retry, so the server acts on it exactly once.
      let payload: TurnPayload | null = null;
      for (let attempt = 0; attempt < 2 && !payload; attempt++) {
        try {
          payload = await api<TurnPayload>(`/conversations/${id}/messages`, { body: { text: value, client_message_id: cmid } });
        } catch (e) {
          if (attempt === 1 || (e instanceof ApiError && e.status < 500)) throw e;
        }
      }
      const fresh = await api<TurnPayload>(`/conversations/${id}?after_seq=${Math.max(0, lastSeq)}`);
      absorb({ ...payload!, messages: fresh.messages });
    } catch (e) {
      setMessages((prev) => prev.filter((m) => m.id !== local.id));
      setText(value);
      setError(e instanceof ApiError ? e.message : "Message not sent. Check the connection and try again.");
    } finally {
      setBusy(false);
    }
  }, [id, busy, cancel, lastSeq, absorb]);

  const speech = useSpeechInput("en-IN", (t) => void send(t));

  async function sendPhoto(blob: Blob, name: string) {
    if (!ui?.camera || !id) return;
    const form = new FormData();
    form.append("pose", ui.camera.pose);
    form.append("file", blob, name);
    const payload = await api<TurnPayload>(`/conversations/${id}/photos`, { form });
    absorb(payload);
  }

  async function takePhoto() {
    if (!ui?.camera) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await mirrorRef.current?.capture();
      if (!blob) throw new Error("no-frame");
      await sendPhoto(blob, `${ui.camera.pose}.jpg`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The camera isn't ready. Wait a moment, or upload a photo instead.");
    } finally {
      setBusy(false);
    }
  }

  // For devices without a camera: the photo is read in memory and uploaded, never stored on the device.
  async function uploadPhoto(file: File | undefined) {
    if (!file || !ui?.camera) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      setError("Choose a JPEG, PNG or WebP photo.");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setError("That photo is larger than 10 MB. Choose a smaller one.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await sendPhoto(file, file.name);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Upload failed. Check the connection and try again.");
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void send(text);
  }

  if (!ui) {
    return (<><TopBar /><main className="centre-page"><p className="muted">{error ?? "Loading the session…"}</p></main></>);
  }

  const handedOver = ui.mode === "handed_over";
  const cameraMode = ui.stage === "capture";
  const inputProps = {
    tel: { inputMode: "numeric" as const, placeholder: "10-digit mobile number", autoComplete: "tel-national" },
    otp: { inputMode: "numeric" as const, placeholder: "6-digit code", autoComplete: "one-time-code", maxLength: 6 },
    date: { inputMode: "text" as const, placeholder: "DD-MM-YYYY" },
    text: { inputMode: "text" as const, placeholder: handedOver ? "Message the doctor" : "Type your answer" },
  }[ui.input] ?? {};

  const status = handedOver
    ? <>A doctor is <strong>joining</strong> the conversation</>
    : speech.listening ? <><strong>Listening…</strong> speak now</>
    : speaking ? <>Speaking</>
    : busy ? <>Thinking…</>
    : cameraMode ? <>{ui.camera?.prompt}</>
    : ui.client_first_name ? <>Talking with <strong>{ui.client_first_name}</strong></> : <>Tap the microphone to talk</>;

  return (
    <>
      <TopBar>
        <StageRail step={ui.step ?? (ui.stage === "handed_over" ? null : ui.steps)} />
      </TopBar>
      <main className="session">
        <section className="stagezone" aria-label="Assistant">
          <div className="mirror-wrap">
            <div style={{ display: "grid", gap: "0.9rem", justifyItems: "center", width: "100%" }}>
              <Mirror ref={mirrorRef} mode={cameraMode ? "camera" : "avatar"} listening={speech.listening}
                speaking={speaking} handedOver={handedOver} />
              <p className="mirror-status" aria-live="polite">{status}</p>
              {cameraMode && ui.camera && (
                <div className="poses" aria-label="Photos">
                  {["front", "left", "right"].map((p) => (
                    <span key={p} className={ui.camera!.done.includes(p) ? "ok" : p === ui.camera!.pose ? "next" : ""}>
                      {p}{ui.camera!.done.includes(p) ? " ✓" : ""}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="controls">
            {cameraMode ? (
              <>
                <button className="shutter" onClick={takePhoto} disabled={busy} aria-label={`Take the ${ui.camera?.pose} photo`} />
                <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" hidden
                  onChange={(e) => void uploadPhoto(e.target.files?.[0])} />
                <button className="btn quiet small" onClick={() => fileRef.current?.click()} disabled={busy}>
                  Upload {ui.camera?.pose} photo
                </button>
              </>
            ) : speechInputSupported() ? (
              <button className={`mic${speech.listening ? " on" : ""}`} onClick={speech.listening ? speech.stop : speech.start}
                aria-pressed={speech.listening} aria-label={speech.listening ? "Stop listening" : "Talk"} disabled={busy}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
                  <rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
                </svg>
              </button>
            ) : null}
            {speechOutputSupported() && (
              <button className="btn quiet small" onClick={() => { if (voiceOn) cancel(); setVoiceOn(!voiceOn); }} aria-pressed={voiceOn}>
                {voiceOn ? "Voice on" : "Voice off"}
              </button>
            )}
            <button className="btn quiet small" onClick={() => { cancel(); nav("/"); }}>End session</button>
          </div>
        </section>

        <section className="talk" aria-label="Conversation">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "talk"} onClick={() => setTab("talk")}>Conversation</button>
            <button role="tab" aria-selected={tab === "results"} onClick={() => setTab("results")} disabled={!ui.results_available}>
              Results{ui.results_available && tab !== "results" && <span className="badge">new</span>}
            </button>
            {(!!ui.history_count || !!ui.unread_messages) && (
              <button role="tab" aria-selected={tab === "history"} onClick={() => setTab("history")}>
                History{ui.unread_messages
                  ? <span className="badge" aria-label={`${ui.unread_messages} new messages`}>{ui.unread_messages} new</span>
                  : <span className="badge quiet">{ui.history_count}</span>}
              </button>
            )}
          </div>

          {tab === "talk" ? (
            <div ref={listRef} className="transcript" aria-live="polite">
              {messages.map((m) => (
                <div key={m.id} className={`msg ${m.role}`}>
                  {m.role === "doctor" && <div className="who-line">{m.meta?.doctor ?? "Doctor"}</div>}
                  {m.role === "system" ? m.content : <div className="bubble">{m.content}</div>}
                </div>
              ))}
              {speech.interim && <div className="msg user interim"><div className="bubble">{speech.interim}</div></div>}
              {busy && <div className="typing">…</div>}
            </div>
          ) : tab === "history" ? (
            <HistoryPanel base={`/conversations/${id}`} summaryHref={(v) => `/session/${id}/history/${v}`}
              onUnreadChange={refresh} />
          ) : (
            <ResultsPanel conversationId={id!} version={resultsVersion} />
          )}

          {tab !== "history" && <div>
            {ui.stage === "approval" && !handedOver && (
              <div className="banner wait">A doctor is reviewing the draft plan. This page updates by itself.</div>
            )}
            {handedOver && (
              <div className="banner handed">A doctor has this consultation now. You can keep typing; they'll see it.</div>
            )}
            {ui.summary_available && ui.visit_id && ui.stage !== "booking" && (
              <div className="banner ok">
                Plan approved. <Link to={`/visits/${ui.visit_id}/summary`} state={{ from: location.pathname }}>Open the visit summary</Link>
              </div>
            )}
            <div className="composer">
              {ui.question && (
                <>
                  <div className="question-head">
                    <span>Know your skin</span>
                    <span>{ui.question.index} of {ui.question.total}</span>
                  </div>
                  <div className="progress"><i style={{ width: `${((ui.question.index - 1) / ui.question.total) * 100}%` }} /></div>
                </>
              )}
              {ui.booking && !handedOver && (
                <div className="question-head">
                  <span>Book your treatment: <strong>{ui.booking.procedure}</strong>, about {ui.booking.duration_min} min</span>
                  {ui.booking.remaining > 1 && <span>{ui.booking.remaining} to book</span>}
                </div>
              )}
              {ui.quick_replies.length > 0 && !handedOver && (
                <div className={`chips${ui.booking ? " slots" : ""}`}>
                  {ui.quick_replies.map((q) => (
                    <button key={q} className={`chip${ui.booking && (q === "Later dates" || q === "Book later") ? " quiet" : ""}`}
                      onClick={() => send(q === "Resend code" ? "resend" : q)} disabled={busy}>{q}</button>
                  ))}
                </div>
              )}
              {error && <p className="error-text" role="alert">{error}</p>}
              <form className="send-row" onSubmit={onSubmit}>
                <label className="sr-only" htmlFor="say" style={{ position: "absolute", left: -9999 }}>Your message</label>
                <input id="say" type="text" value={text} onChange={(e) => setText(e.target.value)} disabled={busy}
                  {...inputProps} />
                <button className="btn" disabled={busy || !text.trim()}>Send</button>
              </form>
            </div>
          </div>}
        </section>
      </main>
    </>
  );
}
