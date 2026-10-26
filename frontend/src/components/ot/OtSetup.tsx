import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../../lib/api";
import { hhmm, shortDay, dayIn, WEEKDAYS } from "../../lib/clinicTime";
import type { DoctorRow, Equipment, Holiday, OtBlock } from "../../lib/otTypes";

const TYPE_HINT: Record<string, string> = {
  qswitch_laser: "Q-switched laser", led_panel: "LED panel", microneedling_pen: "Microneedling pen",
  hydrafacial_machine: "HydraFacial machine",
};

function err(e: unknown, fallback: string) {
  return e instanceof ApiError ? e.message : fallback;
}

// Admins manage machines, doctors' working hours and clinic holidays; doctors can read them.
export default function OtSetup({ isAdmin, tz }: { isAdmin: boolean; tz: string }) {
  return (
    <div className="ot-setup">
      <Machines isAdmin={isAdmin} />
      <DoctorHours isAdmin={isAdmin} />
      <Holidays isAdmin={isAdmin} />
      <Blocks tz={tz} />
    </div>
  );
}

function Machines({ isAdmin }: { isAdmin: boolean }) {
  const [rows, setRows] = useState<Equipment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ code: "", name: "", equipment_type: "", buffer_minutes: 0 });
  const load = useCallback(() => { api<Equipment[]>("/ot/equipment").then(setRows).catch((e) => setError(err(e, "Couldn't load machines."))); }, []);
  useEffect(load, [load]);

  async function patch(e: Equipment, body: Partial<Equipment>) {
    try { await api(`/ot/equipment/${e.id}`, { method: "PATCH", body }); load(); } catch (x) { setError(err(x, "Couldn't save.")); }
  }
  async function add(ev: FormEvent) {
    ev.preventDefault();
    setError(null);
    try {
      await api("/ot/equipment", { body: { ...form, equipment_type: form.equipment_type.trim().toLowerCase().replace(/\s+/g, "_") } });
      setForm({ code: "", name: "", equipment_type: "", buffer_minutes: 0 });
      load();
    } catch (x) { setError(err(x, "Couldn't add the machine.")); }
  }
  const types = [...new Set((rows ?? []).map((r) => r.equipment_type))];

  return (
    <section className="ot-card" aria-labelledby="mach-h">
      <h3 id="mach-h">Machines</h3>
      <p className="tiny">A treatment that needs a machine type reserves one free machine of that type, plus its cool-down or sterilisation time. Out-of-service machines are never booked.</p>
      <table className="log">
        <thead><tr><th>Code</th><th>Machine</th><th>Type</th><th>Buffer</th><th>In service</th></tr></thead>
        <tbody>
          {rows?.map((e) => (
            <tr key={e.id}>
              <td>{e.code}</td><td>{e.name}</td><td>{TYPE_HINT[e.equipment_type] ?? e.equipment_type}</td>
              <td>{isAdmin ? <input type="number" min={0} max={240} className="num" defaultValue={e.buffer_minutes} aria-label={`Buffer minutes for ${e.name}`}
                onBlur={(x) => Number(x.target.value) !== e.buffer_minutes && patch(e, { buffer_minutes: Number(x.target.value) })} /> : `${e.buffer_minutes} min`}</td>
              <td>{isAdmin ? <input type="checkbox" checked={e.active} onChange={(x) => patch(e, { active: x.target.checked })} aria-label={`${e.name} in service`} />
                : e.active ? "Yes" : "No"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {isAdmin && (
        <form className="inline-form" onSubmit={add}>
          <input type="text" placeholder="Code, e.g. EQ-5" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} aria-label="Code" required />
          <input type="text" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-label="Name" required />
          <input type="text" list="eq-types" placeholder="Type" value={form.equipment_type} onChange={(e) => setForm({ ...form, equipment_type: e.target.value })} aria-label="Type" required />
          <datalist id="eq-types">{types.map((t) => <option key={t} value={t}>{TYPE_HINT[t] ?? t}</option>)}</datalist>
          <input type="number" min={0} max={240} className="num" value={form.buffer_minutes} onChange={(e) => setForm({ ...form, buffer_minutes: Number(e.target.value) })} aria-label="Buffer minutes" />
          <button className="btn small">Add machine</button>
        </form>
      )}
      {error && <p className="error-text" role="alert">{error}</p>}
    </section>
  );
}

function DoctorHours({ isAdmin }: { isAdmin: boolean }) {
  const [rows, setRows] = useState<DoctorRow[] | null>(null);
  const [edit, setEdit] = useState<Record<string, { on: boolean; start: string; end: string }[]>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback(() => {
    api<DoctorRow[]>("/ot/doctors").then((d) => {
      setRows(d);
      const e: typeof edit = {};
      d.forEach((doc) => {
        e[doc.id] = WEEKDAYS.map((_, wd) => {
          const h = doc.hours.find((x) => x.weekday === wd);
          return h ? { on: true, start: h.start_time, end: h.end_time } : { on: false, start: "10:00", end: "19:00" };
        });
      });
      setEdit(e);
    }).catch((x) => setMsg({ ok: false, text: err(x, "Couldn't load doctors.") }));
  }, []);
  useEffect(load, [load]);

  async function save(doc: DoctorRow) {
    setMsg(null);
    const hours = edit[doc.id].map((d, wd) => ({ ...d, weekday: wd })).filter((d) => d.on)
      .map((d) => ({ weekday: d.weekday, start_time: d.start, end_time: d.end }));
    try {
      await api(`/ot/doctors/${doc.id}/hours`, { method: "PUT", body: { hours } });
      setMsg({ ok: true, text: `Saved ${doc.full_name}'s hours.` });
      load();
    } catch (x) { setMsg({ ok: false, text: err(x, "Couldn't save the hours.") }); }
  }

  return (
    <section className="ot-card" aria-labelledby="hrs-h">
      <h3 id="hrs-h">Doctors' working hours</h3>
      <p className="tiny">Treatments that need a doctor are only offered when a doctor is working and free. Leave is added as a block below or from the calendar.</p>
      {rows?.map((doc) => (
        <div key={doc.id} className="hours-doc">
          <strong>{doc.full_name}{doc.active ? "" : " (inactive)"}</strong>
          <div className="hours-grid">
            {edit[doc.id]?.map((d, wd) => (
              <div key={wd} className={`hours-day${d.on ? "" : " off"}`}>
                <label><input type="checkbox" checked={d.on} disabled={!isAdmin}
                  onChange={(e) => setEdit({ ...edit, [doc.id]: edit[doc.id].map((x, i) => i === wd ? { ...x, on: e.target.checked } : x) })} /> {WEEKDAYS[wd]}</label>
                {d.on && (
                  <span className="hours-times">
                    <input type="time" value={d.start} disabled={!isAdmin} aria-label={`${WEEKDAYS[wd]} start`}
                      onChange={(e) => setEdit({ ...edit, [doc.id]: edit[doc.id].map((x, i) => i === wd ? { ...x, start: e.target.value } : x) })} />
                    <input type="time" value={d.end} disabled={!isAdmin} aria-label={`${WEEKDAYS[wd]} end`}
                      onChange={(e) => setEdit({ ...edit, [doc.id]: edit[doc.id].map((x, i) => i === wd ? { ...x, end: e.target.value } : x) })} />
                  </span>
                )}
              </div>
            ))}
          </div>
          {isAdmin && <button className="btn small" onClick={() => save(doc)}>Save hours</button>}
        </div>
      ))}
      {msg && <p className={msg.ok ? "tiny ok-text" : "error-text"} role="status">{msg.text}</p>}
    </section>
  );
}

function Holidays({ isAdmin }: { isAdmin: boolean }) {
  const [rows, setRows] = useState<Holiday[]>([]);
  const [day, setDay] = useState("");
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback(() => { api<Holiday[]>("/ot/holidays").then(setRows).catch(() => undefined); }, []);
  useEffect(load, [load]);
  const today = new Date().toISOString().slice(0, 10);

  async function add(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    try {
      const r = await api<{ affected_bookings: number; affected: { label: string }[] }>("/ot/holidays", { body: { day, name } });
      setMsg({ ok: true, text: r.affected_bookings
        ? `Added. ${r.affected_bookings} booking(s) that day still stand; move them if needed: ${r.affected.map((a) => a.label).join(", ")}.`
        : "Added. No bookings that day." });
      setDay(""); setName("");
      load();
    } catch (x) { setMsg({ ok: false, text: err(x, "Couldn't add the holiday.") }); }
  }
  async function remove(d: string) {
    try { await api(`/ot/holidays/${d}`, { method: "DELETE" }); load(); } catch (x) { setMsg({ ok: false, text: err(x, "Couldn't remove it.") }); }
  }

  const upcoming = rows.filter((h) => h.day >= today);
  return (
    <section className="ot-card" aria-labelledby="hol-h">
      <h3 id="hol-h">Clinic holidays</h3>
      <p className="tiny">No times are offered on a holiday. Staff can still book one with a written reason.</p>
      {!upcoming.length && <p className="tiny">No upcoming holidays.</p>}
      <ul className="plain-list">
        {upcoming.map((h) => (
          <li key={h.day}><strong>{shortDay(h.day)}</strong> {h.name}
            {isAdmin && <button className="linkish" onClick={() => remove(h.day)}>Remove</button>}</li>
        ))}
      </ul>
      {isAdmin && (
        <form className="inline-form" onSubmit={add}>
          <input type="date" value={day} min={today} onChange={(e) => setDay(e.target.value)} aria-label="Day" required />
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Diwali" aria-label="Holiday name" required />
          <button className="btn small">Add holiday</button>
        </form>
      )}
      {msg && <p className={msg.ok ? "tiny ok-text" : "error-text"} role="status">{msg.text}</p>}
    </section>
  );
}

function Blocks({ tz }: { tz: string }) {
  const [rows, setRows] = useState<OtBlock[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => { api<OtBlock[]>("/ot/blocks").then(setRows).catch(() => undefined); }, []);
  useEffect(load, [load]);
  async function remove(id: string) {
    try { await api(`/ot/blocks/${id}`, { method: "DELETE" }); load(); } catch (x) { setError(err(x, "Couldn't remove it.")); }
  }
  const what = { room: "Room", doctor: "Leave", equipment: "Machine" } as const;
  return (
    <section className="ot-card" aria-labelledby="blk-h">
      <h3 id="blk-h">Blocked time</h3>
      <p className="tiny">Room maintenance, doctors' leave and machine servicing. Add new blocks from the calendar.</p>
      {!rows.length && <p className="tiny">Nothing blocked from now on.</p>}
      <ul className="plain-list">
        {rows.map((k) => (
          <li key={k.id}>
            <span className="pill quiet">{what[k.target]}</span> <strong>{k.target_name}</strong>{" "}
            {shortDay(dayIn(k.starts_at, tz))} {hhmm(k.starts_at, tz)} – {dayIn(k.ends_at, tz) !== dayIn(k.starts_at, tz)
              ? `${shortDay(dayIn(k.ends_at, tz))} ` : ""}{hhmm(k.ends_at, tz)} · {k.reason}
            <button className="linkish" onClick={() => remove(k.id)}>Remove</button>
          </li>
        ))}
      </ul>
      {error && <p className="error-text">{error}</p>}
    </section>
  );
}
