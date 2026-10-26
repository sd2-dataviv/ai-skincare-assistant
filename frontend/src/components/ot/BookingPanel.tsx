import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, uid } from "../../lib/api";
import { dayIn, hhmm, localIso } from "../../lib/clinicTime";
import type { BookingDetail, ChecklistItem, Conflict, DoctorRow, OtRoom } from "../../lib/otTypes";
import { RULE_LABEL, STAGE_LABEL } from "../../lib/otTypes";

const STATUS_TEXT: Record<ChecklistItem["status"], string> = {
  pending: "To do", done: "Done", not_applicable: "Not needed", blocked: "Blocked",
};

function errText(e: unknown, fallback: string) {
  return e instanceof ApiError ? e.message : fallback;
}

// The selected booking: who/what/where, the pre-treatment checklist, and every action staff can take on it.
// Every change is sent with the version on screen; if someone else changed it first, the server refuses and we reload.
export default function BookingPanel({ bookingId, tz, rooms, onChanged, onClose }: {
  bookingId: string;
  tz: string;
  rooms: OtRoom[];
  onChanged: () => void;
  onClose: () => void;
}) {
  const [b, setB] = useState<BookingDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [mode, setMode] = useState<"view" | "move" | "cancel">("view");

  const load = useCallback(() => {
    api<BookingDetail>(`/ot/bookings/${bookingId}`).then((d) => { setB(d); setError(null); })
      .catch((e) => setError(errText(e, "Couldn't load the booking.")));
  }, [bookingId]);
  useEffect(() => { setMode("view"); setNote(null); load(); }, [load]);

  async function act(path: string, body: unknown, done?: string, method?: string) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const d = await api<BookingDetail & { notified?: boolean }>(path, { body, method });
      setB(d);
      setNote(done ?? null);
      onChanged();
    } catch (e) {
      if (e instanceof ApiError && e.code === "stale_version") {
        setError("Someone else changed this booking just now. It has been reloaded; check it and try again.");
        load();
      } else {
        setError(errText(e, "That didn't work."));
      }
    } finally {
      setBusy(false);
    }
  }

  if (!b) return <div className="ot-card"><p className={error ? "error-text" : "muted"}>{error ?? "Loading…"}</p></div>;
  const r = b.readiness;
  const live = b.status === "tentative" || b.status === "confirmed";
  const nowIso = new Date().toISOString();

  return (
    <div className="ot-card booking-panel" aria-live="polite">
      <div className="bp-head">
        <h3>{b.procedure}</h3>
        <button className="linkish" onClick={onClose} aria-label="Close">Close</button>
      </div>
      <div className="bp-tags">
        <span className={`pill ${b.status === "tentative" ? "tent" : b.status === "confirmed" ? "conf" : ""}`}>
          {b.status === "tentative" ? "Tentative" : b.status === "confirmed" ? "Confirmed" : b.status}
        </span>
        {live && <span className="pill quiet">{STAGE_LABEL[b.stage]}</span>}
        {!!b.overridden_rules.length && (
          <span className="pill warn" title={b.override_reason ?? ""}>Override: {b.overridden_rules.map((x) => RULE_LABEL[x] ?? x).join(", ")}</span>
        )}
      </div>
      <dl className="kv">
        <dt>Client</dt><dd>{b.client_name}</dd>
        <dt>When</dt><dd>{b.label.split(", ")[0]}, {hhmm(b.starts_at, tz)}–{hhmm(b.ends_at, tz)}</dd>
        <dt>Room</dt><dd>{b.room_name}</dd>
        {(b.doctor_name || b.needs_doctor) && <><dt>Doctor</dt><dd>{b.doctor_name ?? "Not assigned"}</dd></>}
        {(b.equipment_name || b.equipment_type) && <><dt>Machine</dt><dd>{b.equipment_name ?? "Not assigned"}</dd></>}
        <dt>Session</dt><dd>{b.session_index} of {b.sessions_total}</dd>
        <dt>Booked by</dt><dd>{b.source === "ai" ? "The client, in the AI chat" : "Staff"}
          {b.confirmed_by ? `. Confirmed by ${b.confirmed_by}` : ""}</dd>
        {b.override_reason && <><dt>Override reason</dt><dd>{b.override_reason}</dd></>}
        {b.started_at && <><dt>Started</dt><dd>{hhmm(b.started_at, tz)}{b.finished_at ? `, finished ${hhmm(b.finished_at, tz)}` : ""}</dd></>}
      </dl>

      {live && b.status === "tentative" && (
        <div className="alert amber">
          <p><strong>Waiting for confirmation.</strong> The client chose this time in the chat. Confirming texts them.</p>
          <div className="actions-bar" style={{ marginTop: "0.5rem" }}>
            <button className="btn small" disabled={busy} onClick={() => act(`/ot/bookings/${b.id}/confirm`, { version: b.version },
              "Confirmed. The client has been texted.")}>Confirm booking</button>
          </div>
        </div>
      )}

      {live && b.checklist.length > 0 && (
        <section className="bp-check" aria-labelledby={`chk-${b.id}`}>
          <div className="bp-check-head">
            <h4 id={`chk-${b.id}`}>Before the treatment</h4>
            {r && <span className={`tiny ${r.complete ? "ok-text" : ""}`}>{r.complete ? "Ready" : `${r.open_mandatory} required left`}</span>}
          </div>
          <ul className="checklist">
            {b.checklist.map((i) => (
              <ChecklistRow key={i.key} item={i} disabled={busy || ["in_progress", "completed", "no_show"].includes(b.stage)}
                onSet={(status, n) => act(`/ot/bookings/${b.id}/readiness/${i.key}`, { status, note: n }, undefined, "PATCH")} />
            ))}
          </ul>
        </section>
      )}

      {error && <p className="error-text" role="alert">{error}</p>}
      {note && <p className="tiny ok-text" role="status">{note}</p>}

      {live && mode === "view" && (
        <div className="actions-bar">
          {b.stage === "scheduled" && b.status === "confirmed" && (
            <button className="btn small" disabled={busy} onClick={() => act(`/ot/bookings/${b.id}/stage`, { stage: "checked_in", version: b.version })}>
              Check in</button>
          )}
          {b.stage === "checked_in" && (
            <button className="btn small" disabled={busy || !r?.complete}
              title={r?.complete ? "" : "Complete the required checklist items first"}
              onClick={() => act(`/ot/bookings/${b.id}/stage`, { stage: "in_progress", version: b.version })}>Start treatment</button>
          )}
          {b.stage === "in_progress" && (
            <button className="btn small" disabled={busy} onClick={() => act(`/ot/bookings/${b.id}/stage`, { stage: "completed", version: b.version })}>
              Finish</button>
          )}
          {(b.stage === "scheduled" || b.stage === "checked_in") && b.starts_at < nowIso && (
            <button className="btn quiet small" disabled={busy} onClick={() => act(`/ot/bookings/${b.id}/stage`, { stage: "no_show", version: b.version })}>
              No-show</button>
          )}
          {b.stage === "scheduled" && <button className="btn quiet small" disabled={busy} onClick={() => setMode("move")}>Move</button>}
          {!["in_progress", "completed"].includes(b.stage) && (
            <button className="btn quiet small danger-text" disabled={busy} onClick={() => setMode("cancel")}>Cancel booking</button>
          )}
          <Link className="btn quiet small" to={`/doctor/visit/${b.visit_id}?tab=records`}>Open visit</Link>
        </div>
      )}
      {b.stage === "checked_in" && !r?.complete && mode === "view" && (
        <p className="tiny">Start treatment unlocks when every required item is done or marked not needed.</p>
      )}

      {mode === "move" && (
        <MoveForm b={b} tz={tz} rooms={rooms} busy={busy} onCancel={() => setMode("view")}
          onMove={async (body) => {
            setBusy(true);
            setError(null);
            try {
              const d = await api<BookingDetail>(`/ot/bookings/${b.id}`, { method: "PATCH", body: { ...body, version: b.version },
                idempotencyKey: uid() });
              setB(d);
              setMode("view");
              setNote("Moved.");
              onChanged();
              return null;
            } catch (e) {
              if (e instanceof ApiError && e.code === "stale_version") load();
              return errText(e, "Couldn't move the booking.");
            } finally {
              setBusy(false);
            }
          }} />
      )}
      {mode === "cancel" && (
        <CancelForm busy={busy} onBack={() => setMode("view")} onCancel={async (reason) => {
          await act(`/ot/bookings/${b.id}/cancel`, { reason, version: b.version }, "Cancelled.");
          setMode("view");
        }} />
      )}
    </div>
  );
}

function ChecklistRow({ item, disabled, onSet }: {
  item: ChecklistItem; disabled: boolean;
  onSet: (status: ChecklistItem["status"], note?: string) => void;
}) {
  const [asking, setAsking] = useState<"not_applicable" | "blocked" | null>(null);
  const [why, setWhy] = useState("");
  return (
    <li className={`chk ${item.status}`}>
      <label className="chk-main">
        <input type="checkbox" checked={item.status === "done"} disabled={disabled}
          onChange={(e) => onSet(e.target.checked ? "done" : "pending")} />
        <span>
          {item.label}{!item.mandatory && <span className="tiny"> (optional)</span>}
          {item.status !== "pending" && item.status !== "done" && <span className={`pill ${item.status === "blocked" ? "warn" : "quiet"}`}>{STATUS_TEXT[item.status]}</span>}
          {item.note && <span className="tiny chk-note">{item.note}</span>}
          {item.updated_by && item.status !== "pending" && <span className="tiny chk-note">{item.updated_by}</span>}
        </span>
      </label>
      {!disabled && item.status === "pending" && !asking && (
        <span className="chk-alt">
          <button className="linkish" onClick={() => setAsking("not_applicable")}>Not needed</button>
          <button className="linkish" onClick={() => setAsking("blocked")}>Blocked</button>
        </span>
      )}
      {!disabled && (item.status === "not_applicable" || item.status === "blocked") && (
        <span className="chk-alt"><button className="linkish" onClick={() => onSet("pending")}>Reopen</button></span>
      )}
      {asking && (
        <form className="chk-why" onSubmit={(e) => { e.preventDefault(); onSet(asking, why); setAsking(null); setWhy(""); }}>
          <input type="text" value={why} onChange={(e) => setWhy(e.target.value)} autoFocus maxLength={300}
            placeholder={asking === "blocked" ? "What's stopping it?" : "Why isn't it needed?"} aria-label="Reason" />
          <button className="btn small" disabled={!why.trim()}>Save</button>
          <button type="button" className="linkish" onClick={() => setAsking(null)}>Back</button>
        </form>
      )}
    </li>
  );
}

function MoveForm({ b, tz, rooms, busy, onCancel, onMove }: {
  b: BookingDetail; tz: string; rooms: OtRoom[]; busy: boolean; onCancel: () => void;
  onMove: (body: Record<string, unknown>) => Promise<string | null>;
}) {
  const [day, setDay] = useState(dayIn(b.starts_at, tz));
  const [time, setTime] = useState(hhmm(b.starts_at, tz));
  const [roomId, setRoomId] = useState(b.room_id ?? "");
  const [doctorId, setDoctorId] = useState(b.doctor_id ?? "");
  const [doctors, setDoctors] = useState<DoctorRow[]>([]);
  const [check, setCheck] = useState<{ ok: boolean; needs_override: boolean; conflicts: Conflict[]; doctor_name: string | null; equipment_name: string | null } | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const suitable = rooms.filter((r) => r.active);

  useEffect(() => { api<DoctorRow[]>("/ot/doctors").then((d) => setDoctors(d.filter((x) => x.active))).catch(() => undefined); }, []);
  useEffect(() => {
    setCheck(null);
    if (!day || !time || !roomId) return;
    const t = setTimeout(() => {
      api<typeof check>("/ot/check", { body: { item_id: b.item_id, room_id: roomId, starts_at: localIso(day, time, tz),
        doctor_id: doctorId || null, booking_id: b.id } }).then(setCheck).catch((e) => setError(errText(e, "Couldn't check that time.")));
    }, 250);
    return () => clearTimeout(t);
  }, [day, time, roomId, doctorId, b.item_id, b.id, tz]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(await onMove({ starts_at: localIso(day, time, tz), room_id: roomId, doctor_id: doctorId || null,
      override_reason: check?.needs_override ? reason.trim() : null }));
  }

  const hard = check?.conflicts.filter((c) => !c.overridable) ?? [];
  const soft = check?.conflicts.filter((c) => c.overridable) ?? [];
  return (
    <form className="move-form" onSubmit={submit}>
      <h4>Move this booking</h4>
      <div className="ot-row2">
        <div className="field"><label htmlFor="mv-day">Day</label><input id="mv-day" type="date" value={day} onChange={(e) => setDay(e.target.value)} /></div>
        <div className="field"><label htmlFor="mv-time">Start</label><input id="mv-time" type="time" step={900} value={time} onChange={(e) => setTime(e.target.value)} /></div>
      </div>
      <div className="field">
        <label htmlFor="mv-room">Room</label>
        <select id="mv-room" value={roomId} onChange={(e) => setRoomId(e.target.value)}>
          {suitable.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.code})</option>)}
        </select>
      </div>
      {(b.needs_doctor || b.doctor_id) && (
        <div className="field">
          <label htmlFor="mv-doc">Doctor</label>
          <select id="mv-doc" value={doctorId} onChange={(e) => setDoctorId(e.target.value)}>
            <option value="">Pick a free doctor automatically</option>
            {doctors.map((d) => <option key={d.id} value={d.id}>{d.full_name}</option>)}
          </select>
        </div>
      )}
      {check && check.ok && <p className="tiny ok-text">Free{check.doctor_name ? ` with ${check.doctor_name}` : ""}{check.equipment_name ? `, ${check.equipment_name}` : ""}.</p>}
      {!!hard.length && <ul className="conflicts">{hard.map((c) => <li key={c.code}>{c.message}</li>)}</ul>}
      {!hard.length && !!soft.length && (
        <>
          <ul className="conflicts soft">{soft.map((c) => <li key={c.code}>{c.message}</li>)}</ul>
          <div className="field">
            <label htmlFor="mv-why">Reason to book anyway (recorded)</label>
            <input id="mv-why" type="text" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300}
              placeholder="e.g. Doctor agreed to stay late" />
          </div>
        </>
      )}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="actions-bar">
        <button className="btn small" disabled={busy || !check || !!hard.length || (!!soft.length && !reason.trim())}>
          {soft.length && !hard.length ? "Move with override" : "Move"}</button>
        <button type="button" className="btn quiet small" onClick={onCancel}>Back</button>
      </div>
    </form>
  );
}

function CancelForm({ busy, onBack, onCancel }: { busy: boolean; onBack: () => void; onCancel: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  return (
    <form className="move-form" onSubmit={(e) => { e.preventDefault(); onCancel(reason.trim() || "Cancelled from the room calendar"); }}>
      <div className="field">
        <label htmlFor="cx-why">Why is it cancelled?</label>
        <input id="cx-why" type="text" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200}
          placeholder="e.g. Client unwell" autoFocus />
      </div>
      <div className="actions-bar">
        <button className="btn danger small" disabled={busy}>Cancel booking</button>
        <button type="button" className="btn quiet small" onClick={onBack}>Keep it</button>
      </div>
    </form>
  );
}
