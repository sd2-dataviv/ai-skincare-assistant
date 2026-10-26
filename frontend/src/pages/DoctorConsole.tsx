import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Appointments, AuthedImage, Findings, formatDate, PlanList, TopBar } from "../components/Chrome";
import { StaffModelPictures } from "../components/ModelPictures";
import { api, ApiError, uid } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Appointment, CatalogueItem, Finding, Message, ModelImageStaff, PlanItem, StaffThread, ThreadMessage, ThreadView } from "../lib/types";

interface QueueItem {
  type: "approval" | "escalation" | "with_doctor" | "message";
  thread_id?: string;
  urgent?: boolean;
  id: string;
  visit_id: string | null;
  conversation_id?: string | null;
  client_name: string;
  age: number | null;
  programme: string | null;
  session_number: number | null;
  summary: string;
  trigger?: string;
  details?: string | null;
  waiting_minutes: number;
  status: string;
  assigned_to?: string | null;
}

const CONCERN_LABEL: Record<string, string> = {
  acne: "Acne", pigmentation: "Pigmentation", wrinkles: "Fine lines", dark_circles: "Dark circles", redness: "Redness",
  pores: "Pores", dullness: "Dullness", dryness: "Dryness",
};

function waited(m: number) {
  if (m < 1) return "just now";
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export default function DoctorConsole() {
  const { kind, itemId } = useParams<{ kind?: string; itemId?: string }>();
  const [params] = useSearchParams();
  const tab = (["records", "messages"] as const).find((t) => t === params.get("tab")) ?? "queue";
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const load = useCallback(() => {
    api<{ items: QueueItem[] }>("/doctor/queue").then((q) => setQueue(q.items)).catch((e) =>
      setError(e instanceof ApiError ? e.message : "Couldn't load the queue."));
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load, tick]);

  const selected = queue?.find((q) => q.id === itemId) ?? null;
  const threadId = kind === "message" || kind === "thread" ? itemId ?? null : null;
  const visitId = threadId ? null : kind === "visit" || kind === "with_doctor" ? itemId : selected?.visit_id ?? null;
  const tabLink = (t: "queue" | "records" | "messages") => `/doctor${kind && itemId ? `/${kind}/${itemId}` : ""}${t !== "queue" ? `?tab=${t}` : ""}`;
  const waitingChats = queue?.filter((q) => q.type === "message").length ?? 0;

  return (
    <>
      <TopBar />
      <main className="console">
        <nav className="queue" aria-label={tab === "queue" ? "Review queue" : "Records"}>
          <div className="queue-tabs" role="tablist">
            <Link role="tab" aria-selected={tab === "queue"} to={tabLink("queue")}>
              Review queue{queue && queue.length > 0 && <span className="count">{queue.length}</span>}
            </Link>
            <Link role="tab" aria-selected={tab === "messages"} to={tabLink("messages")}>
              Messages{waitingChats > 0 && <span className="count">{waitingChats}</span>}
            </Link>
            <Link role="tab" aria-selected={tab === "records"} to={tabLink("records")}>Records</Link>
          </div>
          {tab === "records" ? <RecordsList activeId={itemId ?? null} />
            : tab === "messages" ? <ThreadsList activeId={threadId} /> : (<>
          {error && <p className="error-text" style={{ padding: "1rem" }}>{error}</p>}
          {queue && queue.length === 0 && <p className="empty">Nothing waiting. New drafts and hand-overs appear here automatically. Finished cases are under Records.</p>}
          <ul>
            {queue?.map((q) => (
              <li key={q.id}>
                <Link to={`/doctor/${q.type}/${q.id}`} className={`${q.type !== "approval" && (q.type !== "message" || q.urgent) ? "esc" : ""} ${q.id === itemId ? "active" : ""}`}>
                  <span className="q-top"><span>{q.client_name}{q.age != null ? `, ${q.age}` : ""}</span><span className="tiny">{waited(q.waiting_minutes)}</span></span>
                  <span className={`q-kind ${q.type === "approval" || (q.type === "message" && !q.urgent) ? "apr" : "esc"}`}>
                    {q.type === "escalation" ? `Hand-over: ${q.summary}` : q.summary}
                  </span>
                  <span className="q-meta">{q.type === "message" ? `Chat${q.programme ? ` about ${q.programme}, session ${q.session_number}` : " with the clinic"}`
                    : q.programme ? `${q.programme}, session ${q.session_number}` : "Before visit started"}
                    {q.assigned_to ? `. With ${q.assigned_to}` : ""}</span>
                </Link>
              </li>
            ))}
          </ul>
          </>)}
        </nav>
        <section className="detail" aria-live="polite">
          {threadId ? (
            <StaffThreadView key={threadId} threadId={threadId} onChanged={() => setTick((t) => t + 1)} />
          ) : visitId ? (
            <VisitReview key={visitId} visitId={visitId} queueItem={selected} onChanged={() => setTick((t) => t + 1)} />
          ) : kind === "conversation" && itemId ? (
            <ConversationRecord key={itemId} conversationId={itemId} onChanged={() => setTick((t) => t + 1)} />
          ) : selected?.conversation_id ? (
            <ConversationOnly item={selected} onChanged={() => setTick((t) => t + 1)} />
          ) : (
            <div className="empty">{tab === "messages"
              ? <><h2>Select a chat</h2><p className="muted">Clients chat about their past consultations from the clinic device or their phone. The AI answers from the approved plan and hands over here when a person is needed. Start a chat with any client with New message.</p></>
              : tab === "records"
              ? <><h2>Select a record</h2><p className="muted">Every consultation is kept here, whatever happened: the chat, report, plan, decisions, notes and action log.</p></>
              : <><h2>Select a case</h2><p className="muted">Hand-overs are listed first, then drafts in the order they arrived.</p></>}</div>
          )}
        </section>
      </main>
    </>
  );
}

// ── Records ───────────────────────────────────────────────────────────────────
interface RecordRow {
  conversation_id: string;
  visit_id: string | null;
  client_name: string;
  mobile: string | null;
  age: number | null;
  started_at: string;
  programme: string | null;
  session_number: number | null;
  outcome: string;
  outcome_label: string;
  decisions: { status: string; doctor: string | null }[];
  escalations: { trigger: string; label: string; status: string; doctor: string | null }[];
  handled_by: string[];
  treatments_booked: number;
}

const OUTCOME_FILTERS: [string, string][] = [
  ["", "All outcomes"], ["completed", "Completed"], ["approved", "Approved, wrapping up"],
  ["awaiting_approval", "Awaiting approval"], ["escalated", "Handed to a doctor"], ["rejected", "Plan rejected"],
  ["in_progress", "In progress"], ["not_started", "Before a visit started"],
];

function when(iso: string) {
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function RecordsList({ activeId }: { activeId: string | null }) {
  const [q, setQ] = useState("");
  const [outcome, setOutcome] = useState("");
  const [rows, setRows] = useState<RecordRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(50);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (q.trim()) qs.set("q", q.trim());
      if (outcome) qs.set("outcome", outcome);
      api<{ items: RecordRow[]; total: number }>(`/doctor/records?${qs}`)
        .then((r) => { setRows(r.items); setTotal(r.total); setError(null); })
        .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load records."));
    }, 250);
    return () => clearTimeout(t);
  }, [q, outcome, limit]);

  return (
    <>
      <div className="records-filters">
        <input type="text" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search name or mobile" aria-label="Search records" />
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Filter by outcome">
          {OUTCOME_FILTERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <span className="tiny">{rows ? `${total} record${total === 1 ? "" : "s"}` : "Loading…"}</span>
      </div>
      {error && <p className="error-text" style={{ padding: "1rem" }}>{error}</p>}
      {rows && rows.length === 0 && <p className="empty">No records match.</p>}
      <ul>
        {rows?.map((r) => {
          const to = r.visit_id ? `/doctor/visit/${r.visit_id}?tab=records` : `/doctor/conversation/${r.conversation_id}?tab=records`;
          const rejected = r.decisions.some((d) => d.status === "rejected");
          return (
            <li key={r.conversation_id}>
              <Link to={to} className={(r.visit_id ?? r.conversation_id) === activeId ? "active" : ""}>
                <span className="q-top"><span>{r.client_name}{r.age != null ? `, ${r.age}` : ""}</span><span className="tiny">{when(r.started_at)}</span></span>
                <span className={`q-kind rec-${r.outcome}`}>{r.outcome_label}{rejected ? " · plan rejected once" : ""}</span>
                <span className="q-meta">
                  {r.programme ? `${r.programme}, session ${r.session_number}` : r.escalations[0]?.label ?? "No visit"}
                  {r.handled_by.length ? `. ${r.handled_by.join(", ")}` : ""}
                  {r.treatments_booked ? `. ${r.treatments_booked} treatment${r.treatments_booked > 1 ? "s" : ""} booked` : ""}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {rows && rows.length < total && (
        <p style={{ padding: "0.75rem 1.25rem" }}><button className="btn quiet small" onClick={() => setLimit(limit + 50)}>Show more</button></p>
      )}
    </>
  );
}

interface ConversationDetail {
  conversation_id: string;
  mode: string;
  client: { full_name: string; age: number | null; gender: string | null } | null;
  started_at: string;
  escalations: { id: string; trigger: string; label: string; details: string | null; status: string; doctor: string | null; created_at: string; resolved_at: string | null }[];
}

function ConversationRecord({ conversationId, onChanged }: { conversationId: string; onChanged: () => void }) {
  const { user } = useAuth();
  const [d, setD] = useState<ConversationDetail | null>(null);
  const load = useCallback(() => {
    api<ConversationDetail>(`/doctor/conversations/${conversationId}`).then(setD).catch(() => undefined);
  }, [conversationId]);
  useEffect(() => { load(); }, [load]);
  if (!d) return <p className="muted">Loading…</p>;
  const open = d.escalations.filter((e) => e.status !== "resolved");
  return (
    <>
      <div className="detail-head">
        <div>
          <h1>{d.client?.full_name ?? "Unidentified client"}</h1>
          <p className="sub">{[d.client?.age != null ? `${d.client.age} years` : null, d.client?.gender, `Started ${when(d.started_at)}`, "No visit started"].filter(Boolean).join(", ")}</p>
        </div>
      </div>
      {open.map((e) => <EscalationBar key={e.id} esc={e} onChanged={() => { onChanged(); load(); }} doctorName={user?.full_name ?? "doctor"} />)}
      <History escalations={d.escalations} />
      <Transcript conversationId={conversationId} canMessage={open.length > 0 || d.mode === "handed_over"} />
    </>
  );
}

function PreviousVisits({ clientId, currentId }: { clientId: string; currentId: string }) {
  const [visits, setVisits] = useState<{ id: string; programme: string; session_number: number; status: string; concern_text: string | null; created_at: string }[] | null>(null);
  useEffect(() => {
    api<typeof visits>(`/clients/${clientId}/visits`).then(setVisits).catch(() => setVisits([]));
  }, [clientId]);
  const others = (visits ?? []).filter((v) => v.id !== currentId);
  if (!others.length) return null;
  return (
    <section className="section" aria-labelledby="prev-h">
      <h2 id="prev-h">Previous visits ({others.length})</h2>
      <ul className="prev-visits">
        {others.map((v) => (
          <li key={v.id}>
            <Link to={`/doctor/visit/${v.id}?tab=records`}>{when(v.created_at)}</Link>
            <span>{v.programme.replace("_", " ")} programme, session {v.session_number}</span>
            <span className={`tiny rec-${v.status}`}>{v.status.replace(/_/g, " ")}</span>
            {v.concern_text && <span className="tiny">“{v.concern_text}”</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function History({ decisions = [], escalations = [] }: {
  decisions?: { status: string; doctor: string | null; version: number; notes?: string | null; at: string | null }[];
  escalations?: { trigger: string; label?: string; status: string; doctor: string | null; created_at: string; resolved_at: string | null; details?: string | null }[];
}) {
  const rows = [
    ...decisions.map((x) => ({ at: x.at ?? "", text: `Plan v${x.version} ${x.status === "edited" ? "approved with changes" : x.status}${x.doctor ? ` by ${x.doctor}` : ""}${x.notes ? `: "${x.notes}"` : ""}` })),
    ...escalations.map((e) => ({ at: e.created_at, text: `Handed over: ${e.label ?? e.trigger.replace(/_/g, " ")}${e.doctor ? `. Taken by ${e.doctor}` : ""}${e.status === "resolved" && e.resolved_at ? `. Resolved ${when(e.resolved_at)}` : e.status === "open" ? ". Waiting for a doctor" : ". In progress"}` })),
  ].filter((r) => r.at).sort((a, b) => a.at.localeCompare(b.at));
  if (!rows.length) return null;
  return (
    <section className="section" aria-labelledby="hist-h">
      <h2 id="hist-h">History</h2>
      <ol className="history">
        {rows.map((r, i) => <li key={i}><span className="tiny">{when(r.at)}</span> {r.text}</li>)}
      </ol>
    </section>
  );
}

// ── Shared pieces ─────────────────────────────────────────────────────────────
function Transcript({ conversationId, canMessage }: { conversationId: string; canMessage: boolean }) {
  const [msgs, setMsgs] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);
  const count = msgs.length;
  useEffect(() => { boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight }); }, [count]);
  const load = useCallback(() => {
    api<{ messages: Message[] }>(`/doctor/conversations/${conversationId}`).then((r) => setMsgs(r.messages)).catch(() => undefined);
  }, [conversationId]);
  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);
  async function send() {
    if (!text.trim()) return;
    await api(`/doctor/conversations/${conversationId}/messages`, { body: { text } });
    setText("");
    load();
  }
  return (
    <div className="section">
      <h2>Conversation</h2>
      <div className="mini-transcript" ref={boxRef}>
        {msgs.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === "doctor" && <div className="who-line">{m.meta?.doctor}</div>}
            {m.role === "system" ? m.content : <div className="bubble">{m.content}</div>}
          </div>
        ))}
      </div>
      {canMessage && (
        <form className="send-row" onSubmit={(e) => { e.preventDefault(); void send(); }}>
          <input type="text" value={text} onChange={(e) => setText(e.target.value)} placeholder="Message the client" aria-label="Message the client" />
          <button className="btn">Send</button>
        </form>
      )}
    </div>
  );
}

function EscalationBar({ esc, onChanged, doctorName }: { esc: { id: string; trigger: string; details: string | null; status: string }; onChanged: () => void; doctorName: string }) {
  const [busy, setBusy] = useState(false);
  async function act(path: string, body?: unknown) {
    setBusy(true);
    try { await api(path, { body: body ?? {} }); onChanged(); } finally { setBusy(false); }
  }
  return (
    <div className="alert">
      <p><strong>Handed over: {esc.trigger.replace(/_/g, " ")}.</strong> {esc.details}</p>
      <div className="actions-bar" style={{ marginTop: "0.6rem" }}>
        {esc.status === "open" && <button className="btn small" disabled={busy} onClick={() => act(`/doctor/escalations/${esc.id}/take-over`)}>Join as {doctorName}</button>}
        {esc.trigger !== "doctor_rejected" && (
          <button className="btn quiet small" disabled={busy} onClick={() => act(`/doctor/escalations/${esc.id}/resolve`, { hand_back_to_ai: true })}>Hand back to the AI</button>
        )}
        <button className="btn quiet small" disabled={busy} onClick={() => act(`/doctor/escalations/${esc.id}/resolve`, { hand_back_to_ai: false })}>I'll handle it</button>
      </div>
    </div>
  );
}

function ConversationOnly({ item, onChanged }: { item: QueueItem; onChanged: () => void }) {
  const { user } = useAuth();
  return (
    <>
      <div className="detail-head"><div><h1>{item.client_name}</h1><p className="sub">No visit started yet</p></div></div>
      <EscalationBar esc={{ id: item.id, trigger: item.trigger ?? "", details: item.details ?? null, status: item.status }}
        onChanged={onChanged} doctorName={user?.full_name ?? "doctor"} />
      <Transcript conversationId={item.conversation_id!} canMessage />
    </>
  );
}

// ── Visit review ──────────────────────────────────────────────────────────────
interface VisitDetail {
  visit: { id: string; client_id: string; programme: string; session_number: number; status: string; concern_text: string; concerns: string[]; next_session_date: string | null; created_at: string };
  client: { full_name: string; dob: string | null; gender: string | null; source: string };
  assessment: { answers: Record<string, string>; skin_type: string | null; sensitive: boolean; flags: Record<string, any>; completed: boolean } | null;
  photos: { id: string; pose: string; accepted: boolean; quality: { guidance?: string | null } }[];
  analyses: { kind: string; engine: string; result: any; confidence: number }[];
  prescription: { status: string; items: PlanItem[]; safety: { excluded?: { name: string; reason: string }[]; warnings?: string[]; conditions?: string[] }; rationale: string | null; version: number } | null;
  approvals: { status: string; doctor_id: string | null; prescription_version: number; notes: string | null; decided_at: string | null }[];
  escalations: { id: string; trigger: string; details: string | null; status: string; assigned_doctor_id: string | null; created_at: string; resolved_at: string | null }[];
  people: Record<string, string>;
  notes: { id: string; author_type: string; text: string; tags: string[]; created_at: string }[];
  conversation_id: string | null;
  conversation_mode: string | null;
  ot_bookings: Appointment[];
  actions: { id: string; action: string; actor: string; status: string; stage: string | null; reason: string; created_at: string }[];
  model_images?: ModelImageStaff[];
}

function age(dob: string | null) {
  if (!dob) return null;
  const d = new Date(dob);
  const n = new Date();
  return n.getFullYear() - d.getFullYear() - (n < new Date(n.getFullYear(), d.getMonth(), d.getDate()) ? 1 : 0);
}

function VisitReview({ visitId, queueItem, onChanged }: { visitId: string; queueItem: QueueItem | null; onChanged: () => void }) {
  const { user } = useAuth();
  const nav = useNavigate();
  const [d, setD] = useState<VisitDetail | null>(null);
  const [catalogue, setCatalogue] = useState<CatalogueItem[]>([]);
  const [items, setItems] = useState<PlanItem[]>([]);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [addId, setAddId] = useState("");

  const load = useCallback(() => {
    api<VisitDetail>(`/visits/${visitId}`).then((v) => {
      setD(v);
      setItems(v.prescription?.items ?? []);
    }).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the visit."));
  }, [visitId]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { api<CatalogueItem[]>("/catalogue").then(setCatalogue).catch(() => undefined); }, []);

  const diag = d?.analyses.filter((a) => a.kind === "diagnosis").at(-1);
  const anti = d?.analyses.filter((a) => a.kind === "anti_aging").at(-1);
  const findings: Finding[] = useMemo(() => diag ? Object.entries(diag.result.diagnosis as Record<string, { score: number; source?: Finding["source"]; count?: number }>)
    .map(([c, v]) => ({ concern: c, label: CONCERN_LABEL[c] ?? c, score: v.score, source: v.source, count: v.count }))
    .sort((a, b) => b.score - a.score) : [], [diag]);

  if (error) return <p className="error-text">{error}</p>;
  if (!d) return <p className="muted">Loading…</p>;

  const rx = d.prescription;
  const pending = rx?.status === "pending_approval";
  const openEsc = d.escalations.filter((e) => e.status !== "resolved");
  const handedOver = d.conversation_mode === "handed_over" || openEsc.length > 0;
  const edited = JSON.stringify(items.map((i) => [i.catalogue_id, i.sessions])) !== JSON.stringify((rx?.items ?? []).map((i) => [i.catalogue_id, i.sessions]));
  const flags = d.assessment?.flags ?? {};
  const a = age(d.client.dob);

  async function decide(decision: "approve" | "edit" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const body = decision === "edit"
        ? { decision, notes, items: items.map((i) => ({ catalogue_id: i.catalogue_id, sessions: i.sessions ?? undefined, reason: i.reason ?? undefined })) }
        : { decision, notes };
      await api(`/visits/${visitId}/approval/decision`, { body, idempotencyKey: uid() });
      onChanged();
      load();
      if (decision !== "reject") nav("/doctor");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't save the decision.");
    } finally {
      setBusy(false);
    }
  }

  async function prescribe() {
    setBusy(true);
    setError(null);
    try {
      await api(`/doctor/visits/${visitId}/prescribe`, {
        body: { items: items.map((i) => ({ catalogue_id: i.catalogue_id, sessions: i.sessions ?? undefined, reason: i.reason ?? undefined })), rationale: notes || undefined },
        idempotencyKey: uid(),
      });
      onChanged();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't save the plan.");
    } finally {
      setBusy(false);
    }
  }

  function addItem() {
    const c = catalogue.find((x) => x.id === addId);
    if (!c || items.some((i) => i.catalogue_id === c.id)) return;
    setItems([...items, { catalogue_id: c.id, kind: c.kind as PlanItem["kind"], name: c.name, sessions: c.kind === "product" ? null : c.default_sessions, instructions: c.usage || null, reason: "Added by the doctor." }]);
    setAddId("");
  }

  const canEditPlan = rx?.status !== "approved" && (pending || handedOver);

  return (
    <>
      <div className="detail-head">
        <div>
          <h1>{d.client.full_name}</h1>
          <p className="sub">
            {[a != null ? `${a} years` : null, d.client.gender, `${d.visit.programme.replace("_", " ")} programme, session ${d.visit.session_number}`, formatDate(d.visit.created_at)].filter(Boolean).join(", ")}
          </p>
        </div>
        <div className="head-actions">
          <MessageClientButton clientId={d.visit.client_id} visitId={d.visit.id} />
          <span className={rx?.status === "approved" ? "approved-stamp" : "draft-stamp"}>
            {rx ? `Plan v${rx.version}: ${rx.status.replace("_", " ")}` : "No plan yet"}
          </span>
        </div>
      </div>

      {openEsc.map((e) => <EscalationBar key={e.id} esc={e} onChanged={() => { onChanged(); load(); }} doctorName={user?.full_name ?? "doctor"} />)}
      {queueItem?.type === "approval" && !openEsc.length && (
        <div className="alert amber"><p><strong>Approval needed.</strong> The client is waiting on the clinic device.</p></div>
      )}

      <div className="cols">
        {d.conversation_id && <Transcript conversationId={d.conversation_id} canMessage={handedOver} />}
        <div className="section">
          <h2>Assessment</h2>
          <dl className="kv">
            <dt>Concern</dt><dd>{d.visit.concern_text}</dd>
            <dt>Skin type</dt><dd style={{ textTransform: "capitalize" }}>{d.assessment?.skin_type ?? "—"}{d.assessment?.sensitive ? ", sensitive" : ""}</dd>
            <dt>Medication</dt><dd>{d.assessment?.answers.q13 ?? "—"}{flags.medication_tags ? ` (${flags.medication_tags.join(", ")})` : ""}</dd>
            <dt>Allergies</dt><dd>{d.assessment?.answers.q14 ?? "—"}</dd>
            <dt>Pregnancy</dt><dd>{flags.pregnancy ? "Pregnant or trying" : flags.breastfeeding ? "Breast-feeding" : d.assessment?.answers.q15 ? "No" : "Not asked"}</dd>
            {anti?.result?.skin_age != null && (<><dt>Skin age</dt><dd>{anti.result.skin_age}</dd></>)}
            {diag && (<><dt>Confidence</dt><dd>{Math.round(diag.confidence * 100)}% ({diag.engine})</dd></>)}
          </dl>
          {d.photos.filter((p) => p.accepted).length > 0 && (
            <div className="thumbs">
              {d.photos.filter((p) => p.accepted).slice(-3).map((p) => (
                <figure key={p.id}><AuthedImage photoId={p.id} alt={`${p.pose} photo`} /><figcaption>{p.pose}</figcaption></figure>
              ))}
            </div>
          )}
          {findings.length > 0 && <Findings findings={findings} raised={d.visit.concerns} showSource />}
        </div>
      </div>

      <StaffModelPictures visitId={d.visit.id} images={d.model_images ?? []} model={diag?.result?.model ?? null}
        approved={rx?.status === "approved"} onReload={load} />

      <section className="section" aria-labelledby="plan-h">
        <h2 id="plan-h">Plan</h2>
        {rx?.rationale && <p className="muted">{rx.rationale}</p>}
        {canEditPlan ? (
          <div>
            {items.map((i, idx) => (
              <div className="editor-row" key={i.catalogue_id}>
                <div><div className="nm">{i.name}</div><div className="kd">{i.reason}</div></div>
                {i.kind === "product" ? <span className="tiny">Home care</span> : (
                  <input type="number" min={1} max={24} value={i.sessions ?? 1} aria-label={`Sessions for ${i.name}`}
                    onChange={(e) => setItems(items.map((x, j) => j === idx ? { ...x, sessions: Number(e.target.value) } : x))} />
                )}
                <button className="btn quiet small" onClick={() => setItems(items.filter((_, j) => j !== idx))} aria-label={`Remove ${i.name}`}>Remove</button>
              </div>
            ))}
            <div className="send-row" style={{ marginTop: "0.75rem" }}>
              <select value={addId} onChange={(e) => setAddId(e.target.value)} aria-label="Add from catalogue">
                <option value="">Add from the clinic catalogue…</option>
                {catalogue.filter((c) => !items.some((i) => i.catalogue_id === c.id)).map((c) => (
                  <option key={c.id} value={c.id}>{c.name}{c.contraindications.length ? ` (avoid: ${c.contraindications.join(", ").replace(/_/g, " ")})` : ""}</option>
                ))}
              </select>
              <button className="btn quiet" onClick={addItem} disabled={!addId}>Add</button>
            </div>
          </div>
        ) : rx ? <PlanList items={rx.items} /> : <p className="muted">The AI hasn't drafted a plan yet.</p>}
        {!!rx?.safety?.excluded?.length && (
          <p className="tiny">Safety checks removed: {rx.safety.excluded.map((e) => `${e.name} (${e.reason})`).join("; ")}.</p>
        )}
        {!!rx?.safety?.warnings?.length && <ul className="note-list">{rx.safety.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}

        {canEditPlan && (
          <>
            <div className="field">
              <label htmlFor="dnotes">Note for the record (optional)</label>
              <textarea id="dnotes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
            </div>
            {error && <p className="error-text" role="alert">{error}</p>}
            <div className="actions-bar">
              {pending && !edited && <button className="btn" disabled={busy} onClick={() => decide("approve")}>Approve plan</button>}
              {pending && edited && <button className="btn" disabled={busy || !items.length} onClick={() => decide("edit")}>Approve with my changes</button>}
              {!pending && <button className="btn" disabled={busy || !items.length} onClick={prescribe}>Prescribe and approve</button>}
              {pending && <button className="btn danger" disabled={busy} onClick={() => decide("reject")}>Reject and take over</button>}
            </div>
          </>
        )}
        {!!d.ot_bookings?.length && (
          <div className="section">
            <h3>Treatment room bookings</h3>
            <Appointments items={d.ot_bookings} />
            <p className="tiny"><Link to="/ot">Open the room calendar</Link></p>
          </div>
        )}
        {rx?.status === "approved" && (
          <p><Link to={`/visits/${visitId}/summary`}>Open the visit summary</Link>{d.visit.next_session_date ? `. Next session ${formatDate(d.visit.next_session_date)}.` : ""}</p>
        )}
      </section>

      <PreviousVisits clientId={d.visit.client_id} currentId={d.visit.id} />

      <History
        decisions={d.approvals.filter((x) => x.status !== "pending").map((x) => ({ status: x.status, version: x.prescription_version,
          doctor: x.doctor_id ? d.people?.[x.doctor_id] ?? null : null, notes: x.notes, at: x.decided_at }))}
        escalations={d.escalations.map((e) => ({ ...e, doctor: e.assigned_doctor_id ? d.people?.[e.assigned_doctor_id] ?? null : null }))} />

      {!!d.notes?.length && (
        <section className="section" aria-labelledby="notes-h">
          <h2 id="notes-h">Visit notes</h2>
          {d.notes.map((n) => (
            <div key={n.id} className="note-card">
              <p>{n.text}</p>
              <p className="tiny">{n.author_type === "ai" ? "AI assistant" : "Staff"}, {when(n.created_at)}{n.tags.length ? ` · ${n.tags.join(", ")}` : ""}</p>
            </div>
          ))}
        </section>
      )}

      <details>
        <summary>Action log ({d.actions.length})</summary>
        <table className="log">
          <thead><tr><th>Time</th><th>Action</th><th>By</th><th>Stage</th><th>Status</th><th>Reason</th></tr></thead>
          <tbody>
            {d.actions.map((x) => (
              <tr key={x.id}>
                <td>{new Date(x.created_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
                <td>{x.action}</td><td>{x.actor}</td><td>{x.stage ?? ""}</td>
                <td className={`st-${x.status}`}>{x.status}</td><td>{x.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  );
}

// ── Messages: follow-up chats with clients ───────────────────────────────────
function ago(iso: string | null) {
  if (!iso) return "";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min`;
  if (mins < 24 * 60) return `${Math.round(mins / 60)} h`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function MessageClientButton({ clientId, visitId, label = "Message client" }: { clientId: string; visitId?: string | null; label?: string }) {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  async function open() {
    setBusy(true);
    try {
      const t = await api<StaffThread>("/messages/threads", { body: { client_id: clientId, visit_id: visitId ?? null } });
      nav(`/doctor/thread/${t.id}?tab=messages`);
    } finally {
      setBusy(false);
    }
  }
  return <button className="btn quiet small" onClick={open} disabled={busy}>{label}</button>;
}

function ThreadsList({ activeId }: { activeId: string | null }) {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [rows, setRows] = useState<StaffThread[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [found, setFound] = useState<{ id: string; full_name: string; mobile: string }[]>([]);

  const load = useCallback(() => {
    const qs = new URLSearchParams();
    if (q.trim() && !picking) qs.set("q", q.trim());
    if (waiting) qs.set("waiting", "true");
    api<{ threads: StaffThread[] }>(`/messages/threads?${qs}`).then((r) => { setRows(r.threads); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load messages."));
  }, [q, waiting, picking]);
  useEffect(() => {
    const t = setTimeout(load, 250);
    const i = setInterval(load, 6000);
    return () => { clearTimeout(t); clearInterval(i); };
  }, [load]);

  useEffect(() => {
    if (!picking || q.trim().length < 2) { setFound([]); return; }
    const t = setTimeout(() => {
      api<{ id: string; full_name: string; mobile: string }[]>(`/clients?q=${encodeURIComponent(q.trim())}`)
        .then(setFound).catch(() => setFound([]));
    }, 250);
    return () => clearTimeout(t);
  }, [picking, q]);

  async function start(clientId: string) {
    const t = await api<StaffThread>("/messages/threads", { body: { client_id: clientId } });
    setPicking(false);
    setQ("");
    nav(`/doctor/thread/${t.id}?tab=messages`);
  }

  return (
    <>
      <div className="records-filters">
        <input type="text" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder={picking ? "Find a client by name or mobile" : "Search chats by name or mobile"}
          aria-label={picking ? "Find a client" : "Search chats"} autoFocus={picking} />
        <div className="filter-row">
          {!picking && (
            <label className="check"><input type="checkbox" checked={waiting} onChange={(e) => setWaiting(e.target.checked)} /> Waiting for a reply</label>
          )}
          <button className="btn quiet small" onClick={() => { setPicking(!picking); setQ(""); }}>{picking ? "Cancel" : "New message"}</button>
        </div>
      </div>
      {picking ? (
        <ul>
          {q.trim().length < 2 && <p className="empty">Type at least two letters or digits of the client's name or mobile.</p>}
          {q.trim().length >= 2 && !found.length && <p className="empty">No clients match.</p>}
          {found.map((c) => (
            <li key={c.id}>
              <button className="pick-row" onClick={() => void start(c.id)}>
                <strong>{c.full_name}</strong><span className="tiny">••••{c.mobile.slice(-4)}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <>
          {error && <p className="error-text" style={{ padding: "1rem" }}>{error}</p>}
          {rows && rows.length === 0 && <p className="empty">{waiting ? "Nobody is waiting for a reply." : "No chats yet. Use New message to write to any client."}</p>}
          <ul>
            {rows?.map((t) => (
              <li key={t.id}>
                <Link to={`/doctor/thread/${t.id}?tab=messages`}
                  className={`${t.waiting && t.handover_reason === "medical_concern" ? "esc" : ""} ${t.id === activeId ? "active" : ""}`}>
                  <span className="q-top"><span>{t.client.full_name}{t.client.age != null ? `, ${t.client.age}` : ""}</span><span className="tiny">{ago(t.last_message_at)}</span></span>
                  <span className={`q-kind ${t.waiting ? (t.handover_reason === "medical_concern" ? "esc" : "apr") : ""}`}>
                    {t.waiting ? "Waiting for a reply" : t.mode === "ai" ? "AI answering" : `With ${t.assigned_to ?? "the clinic team"}`}
                    {t.unread > 0 ? ` · ${t.unread} new` : ""}
                  </span>
                  <span className="q-meta">{t.visit ? `${t.visit.programme}, session ${t.visit.session_number}` : "General"}
                    {t.last_message ? `: ${t.last_message.content.slice(0, 70)}` : ""}</span>
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function StaffThreadView({ threadId, onChanged }: { threadId: string; onChanged: () => void }) {
  const [t, setT] = useState<StaffThread | null>(null);
  const [msgs, setMsgs] = useState<ThreadMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const lastSeq = msgs.reduce((mx, m) => Math.max(mx, m.seq), 0);

  const merge = useCallback((incoming: ThreadMessage[]) => {
    if (!incoming.length) return;
    setMsgs((prev) => {
      const seen = new Set(prev.map((m) => m.id));
      return [...prev, ...incoming.filter((m) => !seen.has(m.id))].sort((a, b) => a.seq - b.seq);
    });
  }, []);
  useEffect(() => {
    api<ThreadView<StaffThread>>(`/messages/threads/${threadId}`).then((v) => { setT(v.thread); merge(v.messages); onChanged(); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't open the chat."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, merge]);
  useEffect(() => {
    const i = setInterval(() => {
      api<ThreadView<StaffThread>>(`/messages/threads/${threadId}?after_seq=${lastSeq}`)
        .then((v) => { setT(v.thread); merge(v.messages); }).catch(() => undefined);
    }, 4000);
    return () => clearInterval(i);
  }, [threadId, lastSeq, merge]);
  useEffect(() => { boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight }); }, [msgs.length]);

  async function send(e: FormEvent) {
    e.preventDefault();
    const value = text.trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ message: ThreadMessage; notified: boolean; thread: StaffThread }>(`/messages/threads/${threadId}/messages`, { body: { text: value } });
      setText("");
      setT(r.thread);
      merge([r.message]);
      setNote(r.notified ? "Sent. The client has been texted." : "Sent.");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Message not sent.");
    } finally {
      setBusy(false);
    }
  }

  async function handBack() {
    setBusy(true);
    try {
      const r = await api<{ message: ThreadMessage; thread: StaffThread }>(`/messages/threads/${threadId}/hand-back`, { body: {} });
      setT(r.thread);
      merge([r.message]);
      setNote("The AI assistant is answering again.");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't hand back.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !t) return <p className="error-text">{error}</p>;
  if (!t) return <p className="muted">Loading…</p>;
  return (
    <>
      <div className="detail-head">
        <div>
          <h1>{t.client.full_name}</h1>
          <p className="sub">{[t.client.age != null ? `${t.client.age} years` : null, t.client.gender, t.client.mobile,
            t.visit ? `Chat about ${t.visit.programme}, session ${t.visit.session_number} (${formatDate(t.visit.date)})` : "General chat"].filter(Boolean).join(", ")}</p>
        </div>
        <div className="head-actions">
          {t.visit && <Link className="btn quiet small" to={`/doctor/visit/${t.visit.id}?tab=records`}>Open the visit</Link>}
          <span className={t.mode === "ai" ? "draft-stamp" : "approved-stamp"}>{t.mode === "ai" ? "AI answering" : `With ${t.assigned_to ?? "the clinic team"}`}</span>
        </div>
      </div>
      {t.waiting && (
        <div className={`alert${t.handover_reason === "medical_concern" ? "" : " amber"}`}>
          <p><strong>Waiting for a reply{t.handover_text ? `: ${t.handover_text.replace(/\.$/, "")}` : ""}.</strong> {t.handover_details}</p>
        </div>
      )}
      <div className="section">
        <div className="mini-transcript tall" ref={boxRef}>
          {!msgs.length && <p className="msg system">No messages yet. Your message is texted to the client, who can reply from their phone or the clinic device.</p>}
          {msgs.map((m) => (
            <div key={m.id} className={`msg ${m.role === "client" ? "user" : m.role === "staff" ? "doctor" : m.role}`}>
              {m.role === "client" && <div className="who-line">{t.client.full_name.split(" ")[0]}{m.channel ? ` · ${m.channel}` : ""}</div>}
              {m.role === "staff" && <div className="who-line">{m.author}{m.author_role === "clinic team" ? " (clinic team)" : ""}</div>}
              {m.role === "assistant" && <div className="who-line">AI assistant</div>}
              {m.role === "system" ? m.content : <div className="bubble">{m.content}</div>}
            </div>
          ))}
        </div>
        <form className="send-row" onSubmit={send}>
          <input type="text" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000}
            placeholder={`Message ${t.client.full_name.split(" ")[0]}`} aria-label="Message the client" disabled={busy} />
          <button className="btn" disabled={busy || !text.trim()}>Send</button>
        </form>
        {error && <p className="error-text" role="alert">{error}</p>}
        {note && <p className="tiny" role="status">{note}</p>}
        <div className="actions-bar">
          {t.mode === "staff" && <button className="btn quiet small" onClick={handBack} disabled={busy}>Hand back to the AI assistant</button>}
          {t.visit && <MessageClientButton clientId={t.client.id} label="General chat with this client" />}
        </div>
        <p className="tiny">When you write, the AI assistant stops answering in this chat until you hand it back.</p>
      </div>
    </>
  );
}
