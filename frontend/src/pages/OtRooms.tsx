import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { TopBar } from "../components/Chrome";
import BookingPanel from "../components/ot/BookingPanel";
import OtReports from "../components/ot/OtReports";
import OtSetup from "../components/ot/OtSetup";
import { api, ApiError, uid } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dayIn, fmtMin, hhmm, localIso, longDay, minutesIn, shiftDay, shortName, toMin } from "../lib/clinicTime";
import type { DoctorRow, Equipment, OtBooking, Schedule, ToSchedule } from "../lib/otTypes";
import { CAPABILITY, STAGE_LABEL } from "../lib/otTypes";
import type { Slot } from "../lib/types";

const ROW_PX = 40; // one 30-minute row
type Tab = "calendar" | "setup" | "reports";

export default function OtRooms() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const tab: Tab = (["setup", "reports"] as const).find((t) => t === params.get("tab")) ?? "calendar";
  const isAdmin = user?.role === "admin";
  const [tz, setTz] = useState("Asia/Kolkata");
  return (
    <>
      <TopBar />
      <main className="ot-page">
        <header className="ot-head">
          <div>
            <h1>Treatment rooms</h1>
            <div className="queue-tabs ot-tabs" role="tablist">
              {(["calendar", "setup", "reports"] as Tab[]).map((t) => (
                <a key={t} role="tab" aria-selected={tab === t} href={`?tab=${t}`}
                  onClick={(e) => { e.preventDefault(); setParams(t === "calendar" ? {} : { tab: t }); }}>
                  {{ calendar: "Calendar", setup: "Machines, hours & holidays", reports: "Reports" }[t]}
                </a>
              ))}
            </div>
          </div>
        </header>
        {tab === "calendar" && <Calendar onTz={setTz} />}
        {tab === "setup" && <OtSetup isAdmin={isAdmin} tz={tz} />}
        {tab === "reports" && <OtReports tz={tz} />}
      </main>
    </>
  );
}

function Calendar({ onTz }: { onTz: (tz: string) => void }) {
  const [day, setDay] = useState<string | null>(null);
  const [data, setData] = useState<Schedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Schedule>(`/ot/schedule${day ? `?day=${day}` : ""}`).then((s) => {
      setData(s);
      onTz(s.timezone);
      if (!day) setDay(s.day);
      setError(null);
    }).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the room calendar."));
  }, [day, onTz]);
  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const tz = data?.timezone ?? "Asia/Kolkata";
  const [start, end] = useMemo(() => {
    const open = data?.rooms.filter((r) => r.open_today) ?? [];
    const rooms = open.length ? open : data?.rooms ?? [];
    if (!rooms.length) return [600, 1140];
    return [Math.min(...rooms.map((r) => toMin(r.open_time))), Math.max(...rooms.map((r) => toMin(r.close_time)))];
  }, [data]);
  const rows = Math.max(1, Math.ceil((end - start) / 30));
  const top = (iso: string) => ((Math.max(start, minutesIn(iso, tz)) - start) / 30) * ROW_PX;
  const span = (a: string, b: string) => {
    const from = Math.max(start, minutesIn(a, tz));
    const to = dayIn(b, tz) > (data?.day ?? "") ? end : Math.min(end, minutesIn(b, tz));
    return Math.max(ROW_PX * 0.5, ((to - from) / 30) * ROW_PX);
  };
  const all = data?.rooms.flatMap((r) => r.bookings) ?? [];

  async function unblock(id: string) {
    setBusy(true);
    try {
      await api(`/ot/blocks/${id}`, { method: "DELETE" });
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't remove the block.");
    } finally {
      setBusy(false);
    }
  }

  async function confirm(b: OtBooking) {
    setBusy(true);
    try {
      await api(`/ot/bookings/${b.id}/confirm`, { body: { version: b.version } });
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't confirm.");
      load();
    } finally {
      setBusy(false);
    }
  }

  const what = { doctor: "On leave", equipment: "Out for servicing", room: "Blocked" } as const;
  return (
    <>
      <div className="ot-subhead">
        <p className="muted">{day ? longDay(day) : ""}{data ? ` · ${all.length} booked${data.tentative ? `, ${data.tentative} to confirm` : ""} · times in ${tz.replace("_", " ")}` : ""}</p>
        <div className="ot-daynav" role="group" aria-label="Choose a day">
          <button className="btn quiet small" onClick={() => day && setDay(shiftDay(day, -1))} aria-label="Previous day">‹</button>
          <input type="date" value={day ?? ""} onChange={(e) => e.target.value && setDay(e.target.value)} aria-label="Day" />
          <button className="btn quiet small" onClick={() => day && setDay(shiftDay(day, 1))} aria-label="Next day">›</button>
          <button className="btn quiet small" onClick={() => setDay(null)}>Today</button>
        </div>
      </div>
      {error && <p className="error-text" role="alert">{error}</p>}
      {data?.holiday && <div className="alert amber"><p><strong>Clinic holiday: {data.holiday}.</strong> No times are offered to clients today.{all.length ? " Bookings shown were made before it was added, or with an override." : ""}</p></div>}
      {!!data?.other_blocks.length && (
        <p className="ot-away">{data.other_blocks.map((k) => (
          <span key={k.id} className="pill quiet">{what[k.target]}: {k.target_name} {hhmm(k.starts_at, tz)}–{dayIn(k.ends_at, tz) > data.day ? "…" : hhmm(k.ends_at, tz)}</span>
        ))}</p>
      )}

      <div className="ot-layout">
        <section className="ot-cal-wrap" aria-label="Room calendar">
          {!data ? <p className="muted">Loading…</p> : (
            <div className="ot-cal" style={{ gridTemplateColumns: `3.5rem repeat(${data.rooms.length}, minmax(170px, 1fr))` }}>
              <div className="ot-corner" />
              {data.rooms.map((r) => (
                <div key={r.id} className="ot-room-head">
                  <strong>{r.name}</strong>
                  <span className="tiny">{r.code} · {r.capabilities.map((c) => CAPABILITY[c] ?? c).join(", ")}</span>
                </div>
              ))}
              <div className="ot-times" style={{ height: rows * ROW_PX }}>
                {Array.from({ length: rows }, (_, i) => (
                  <span key={i} style={{ top: i * ROW_PX }}>{i % 2 === 0 ? fmtMin(start + i * 30) : ""}</span>
                ))}
              </div>
              {data.rooms.map((r) => (
                <div key={r.id} className={`ot-col${r.open_today ? "" : " closed"}`} style={{ height: rows * ROW_PX }}
                  aria-label={`${r.name} on ${longDay(data.day)}`}>
                  {!r.open_today && <span className="ot-closed">Closed</span>}
                  {r.open_today && toMin(r.open_time) > start && (
                    <div className="ot-shut" style={{ top: 0, height: ((toMin(r.open_time) - start) / 30) * ROW_PX }} />
                  )}
                  {r.open_today && toMin(r.close_time) < end && (
                    <div className="ot-shut" style={{ top: ((toMin(r.close_time) - start) / 30) * ROW_PX, bottom: 0 }} />
                  )}
                  {r.blocks.map((k) => (
                    <div key={k.id} className="ot-block" style={{ top: top(k.starts_at), height: span(k.starts_at, k.ends_at) }}>
                      <span>{k.reason}</span>
                      <span className="tiny">{hhmm(k.starts_at, tz)}–{dayIn(k.ends_at, tz) > data.day ? "…" : hhmm(k.ends_at, tz)}</span>
                      <button className="linkish" disabled={busy} onClick={() => unblock(k.id)}>Remove</button>
                    </div>
                  ))}
                  {r.bookings.map((b) => (
                    <button key={b.id}
                      className={`ot-booking${selected === b.id ? " selected" : ""}${b.tentative ? " tentative" : ""} st-${b.stage}`}
                      style={{ top: top(b.starts_at), height: span(b.starts_at, b.clear_until) }}
                      onClick={() => setSelected(selected === b.id ? null : b.id)}
                      aria-label={`${b.tentative ? "Tentative: " : ""}${b.procedure} for ${b.client_name}, ${hhmm(b.starts_at, tz)} to ${hhmm(b.ends_at, tz)}, ${STAGE_LABEL[b.stage]}`}>
                      <span className="t">{hhmm(b.starts_at, tz)}–{hhmm(b.ends_at, tz)} · {shortName(b.client_name)}</span>
                      <span className="p">{b.procedure}</span>
                      <span className="flags">
                        {b.tentative && <em className="tag tent">Tentative</em>}
                        {b.stage !== "scheduled" && <em className={`tag stage-${b.stage}`}>{STAGE_LABEL[b.stage]}</em>}
                        {b.stage === "scheduled" && !b.tentative && b.readiness && !b.readiness.complete && (
                          <em className="tag">Checklist {b.readiness.done}/{b.readiness.total}</em>
                        )}
                        {b.doctor_name && <em className="tag quiet">{shortName(b.doctor_name)}</em>}
                      </span>
                      <i className="turnover" style={{ height: span(b.ends_at, b.clear_until) }} aria-hidden />
                    </button>
                  ))}
                </div>
              ))}
            </div>
          )}
          <p className="tiny ot-legend">
            <span className="sw booked" /> Confirmed <span className="sw tent" /> Tentative <span className="sw turn" /> Turnover
            <span className="sw blocked" /> Blocked <span className="sw shut" /> Closed
          </p>
        </section>

        <aside className="ot-side">
          {selected && data && (
            <BookingPanel key={selected} bookingId={selected} tz={tz} rooms={data.rooms} onChanged={load}
              onClose={() => setSelected(null)} />
          )}
          <ToConfirm items={data?.to_confirm ?? []} busy={busy} onConfirm={confirm}
            onOpen={(b) => { setDay(dayIn(b.starts_at, tz)); setSelected(b.id); }} />
          <ToBook items={data?.to_schedule ?? []} tz={tz} onBooked={(d) => { setDay(d); load(); }} />
          {data && <BlockForm data={data} day={data.day} tz={tz} onDone={load} />}
        </aside>
      </div>
    </>
  );
}

function ToConfirm({ items, busy, onConfirm, onOpen }: {
  items: OtBooking[]; busy: boolean; onConfirm: (b: OtBooking) => void; onOpen: (b: OtBooking) => void;
}) {
  if (!items.length) return null;
  return (
    <section className="ot-card" aria-labelledby="toconf-h">
      <h3 id="toconf-h">To confirm <span className="tiny">({items.length} chosen in the chat)</span></h3>
      <ul className="ot-todo">
        {items.slice(0, 8).map((b) => (
          <li key={b.id}>
            <div className="ot-todo-row">
              <button className="linkish" onClick={() => onOpen(b)}>{b.client_name}</button>
              <button className="btn small" disabled={busy} onClick={() => onConfirm(b)}>Confirm</button>
            </div>
            <span className="tiny">{b.procedure} · {b.label} · {b.room_name}</span>
          </li>
        ))}
      </ul>
      {items.length > 8 && <p className="tiny">…and {items.length - 8} more. Open a day to see them on the calendar.</p>}
    </section>
  );
}

function ToBook({ items, tz, onBooked }: { items: ToSchedule[]; tz: string; onBooked: (day: string) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const key = (t: ToSchedule) => `${t.visit_id}:${t.item_id}`;
  const visits = useMemo(() => {
    const m = new Map<string, ToSchedule[]>();
    items.forEach((t) => m.set(t.visit_id, [...(m.get(t.visit_id) ?? []), t]));
    return [...m.values()].reverse(); // newest visits first
  }, [items]);
  const shown = showAll ? visits : visits.slice(0, 5);

  async function find(t: ToSchedule) {
    setOpen(key(t));
    setSlots([]);
    setError(null);
    try {
      const r = await api<{ slots: Slot[] }>(`/ot/availability?item_id=${encodeURIComponent(t.item_id)}&count=4`);
      setSlots(r.slots);
      if (!r.slots.length) setError("No free room in the next three weeks.");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't check availability.");
    }
  }

  async function book(t: ToSchedule, s: Slot) {
    setBusy(true);
    setError(null);
    try {
      await api("/ot/bookings", { body: { visit_id: t.visit_id, item_id: t.item_id, room_id: s.room_id, starts_at: s.starts_at },
        idempotencyKey: uid() });
      setOpen(null);
      onBooked(dayIn(s.starts_at, tz));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't book that time.");
      if (e instanceof ApiError && e.status === 409) void find(t);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="ot-card" aria-labelledby="tobook-h">
      <h3 id="tobook-h">To book <span className="tiny">({items.length} treatment{items.length === 1 ? "" : "s"})</span></h3>
      {!items.length && <p className="tiny">Every approved treatment has a room booked.</p>}
      <ul className="ot-todo">
        {shown.map((group) => (
          <li key={group[0].visit_id}>
            <div>
              <strong>{group[0].client_name}</strong>
              <span className="tiny">Visit on {new Date(`${group[0].visit_date}T12:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}
                {group.some((t) => t.deferred) ? " · asked to book later" : ""}</span>
            </div>
            {group.map((t) => (
              <div key={key(t)} className="ot-todo-item">
                <div className="ot-todo-row">
                  <span>{t.procedure} <span className="tiny">{t.duration_min} min</span></span>
                  {open !== key(t) && <button className="btn quiet small" onClick={() => find(t)}>Find a time</button>}
                </div>
                {open === key(t) && (
                  <div className="ot-slots">
                    {slots.map((s) => (
                      <button key={s.starts_at + s.room_id} className="chip" disabled={busy} onClick={() => book(t, s)}>
                        {s.label} <span className="tiny">{s.room_code}</span>
                      </button>
                    ))}
                    {error && <p className="error-text">{error}</p>}
                  </div>
                )}
              </div>
            ))}
          </li>
        ))}
      </ul>
      {visits.length > 5 && (
        <button className="linkish" onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show fewer" : `Show all ${visits.length} clients`}
        </button>
      )}
    </section>
  );
}

function BlockForm({ data, day, tz, onDone }: { data: Schedule; day: string; tz: string; onDone: () => void }) {
  const [target, setTarget] = useState<"room" | "doctor" | "equipment">("room");
  const [targetId, setTargetId] = useState(data.rooms[0]?.id ?? "");
  const [doctors, setDoctors] = useState<DoctorRow[]>([]);
  const [machines, setMachines] = useState<Equipment[]>([]);
  const [from, setFrom] = useState("13:00");
  const [to, setTo] = useState("14:00");
  const [allDay, setAllDay] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api<DoctorRow[]>("/ot/doctors").then((d) => setDoctors(d.filter((x) => x.active))).catch(() => undefined);
    api<Equipment[]>("/ot/equipment").then(setMachines).catch(() => undefined);
  }, []);
  const options = target === "room" ? data.rooms.map((r) => ({ id: r.id, name: `${r.name} (${r.code})` }))
    : target === "doctor" ? doctors.map((d) => ({ id: d.id, name: d.full_name }))
    : machines.map((m) => ({ id: m.id, name: `${m.name} (${m.code})` }));
  useEffect(() => { setTargetId(options[0]?.id ?? ""); }, [target, doctors.length, machines.length]); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    const key = { room: "room_id", doctor: "doctor_id", equipment: "equipment_id" }[target];
    try {
      await api("/ot/blocks", { body: { [key]: targetId, starts_at: localIso(day, allDay ? "00:00" : from, tz),
        ends_at: allDay ? localIso(shiftDay(day, 1), "00:00", tz) : localIso(day, to, tz),
        reason: reason.trim() || { room: "Unavailable", doctor: "Leave", equipment: "Servicing" }[target] } });
      setReason("");
      setMsg({ ok: true, text: "Blocked." });
      onDone();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.message : "Couldn't block that time." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="ot-card" onSubmit={submit} aria-labelledby="block-h">
      <h3 id="block-h">Block time on {longDay(day)}</h3>
      <div className="seg" role="radiogroup" aria-label="What to block">
        {(["room", "doctor", "equipment"] as const).map((t) => (
          <label key={t} className={target === t ? "on" : ""}>
            <input type="radio" name="blk-target" checked={target === t} onChange={() => setTarget(t)} />
            {{ room: "Room", doctor: "Doctor's leave", equipment: "Machine" }[t]}
          </label>
        ))}
      </div>
      <div className="field">
        <label htmlFor="blk-target">{{ room: "Room", doctor: "Doctor", equipment: "Machine" }[target]}</label>
        <select id="blk-target" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
          {options.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
        </select>
      </div>
      <label className="check"><input type="checkbox" checked={allDay} onChange={(e) => setAllDay(e.target.checked)} /> All day</label>
      {!allDay && (
        <div className="ot-row2">
          <div className="field"><label htmlFor="blk-from">From</label><input id="blk-from" type="time" value={from} onChange={(e) => setFrom(e.target.value)} /></div>
          <div className="field"><label htmlFor="blk-to">To</label><input id="blk-to" type="time" value={to} onChange={(e) => setTo(e.target.value)} /></div>
        </div>
      )}
      <div className="field">
        <label htmlFor="blk-why">Reason</label>
        <input id="blk-why" type="text" value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder={{ room: "e.g. Deep clean", doctor: "e.g. Conference", equipment: "e.g. Annual service" }[target]} />
      </div>
      {msg && <p className={msg.ok ? "tiny" : "error-text"} role="status">{msg.text}</p>}
      <button className="btn small" disabled={busy || !targetId || (!allDay && to <= from)}>Block this time</button>
    </form>
  );
}
