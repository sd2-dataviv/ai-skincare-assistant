import { useEffect, useState } from "react";
import { api, ApiError, getToken } from "../../lib/api";
import { shiftDay, todayIn } from "../../lib/clinicTime";
import type { Rate, Report } from "../../lib/otTypes";

const pct = (r: Rate) => (r.value == null ? "—" : `${r.value}%`);
const mins = (m: number | null | undefined) => (m == null ? "—" : `${m} min`);
const hours = (m: number) => `${(m / 60).toFixed(m >= 600 ? 0 : 1)} h`;

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-tile">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

// A ratio against a limit: one hue on a same-hue track; the number is in text ink beside it.
function Meter({ rate, label }: { rate: Rate; label: string }) {
  const v = rate.value ?? 0;
  return (
    <span className="meter" title={`${label}: ${pct(rate)} (${Math.round(rate.num)} of ${Math.round(rate.den)} min)`}>
      <span className="meter-track" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={rate.value ?? undefined}
        aria-label={`${label} ${pct(rate)}`}>
        <i style={{ width: `${Math.min(100, v)}%` }} />
      </span>
      <span className="meter-val">{pct(rate)}</span>
    </span>
  );
}

// Utilisation, turnover, punctuality and outcomes for the treatment rooms, from recorded data.
export default function OtReports({ tz }: { tz: string }) {
  const today = todayIn(tz);
  const [from, setFrom] = useState(shiftDay(today, -29));
  const [to, setTo] = useState(today);
  const [rep, setRep] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setRep(null);
    api<Report>(`/ot/reports?from=${from}&to=${to}`).then((r) => { setRep(r); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the report."));
  }, [from, to]);

  async function downloadCsv() {
    const res = await fetch(`/api/ot/reports.csv?from=${from}&to=${to}`, { headers: { Authorization: `Bearer ${getToken()}` } });
    if (!res.ok) return setError("Couldn't download the CSV.");
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `treatment-rooms-${from}-to-${to}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const t = rep?.total;
  return (
    <div className="ot-reports">
      <div className="report-filters" role="group" aria-label="Report period">
        <label>From <input type="date" value={from} max={to} onChange={(e) => e.target.value && setFrom(e.target.value)} /></label>
        <label>To <input type="date" value={to} min={from} onChange={(e) => e.target.value && setTo(e.target.value)} /></label>
        <button className="btn quiet small" onClick={() => { setFrom(shiftDay(today, -6)); setTo(today); }}>Last 7 days</button>
        <button className="btn quiet small" onClick={() => { setFrom(shiftDay(today, -29)); setTo(today); }}>Last 30 days</button>
        <button className="btn quiet small" onClick={() => { setFrom(today); setTo(shiftDay(today, 13)); }}>Next 2 weeks</button>
        <span className="spacer" />
        <button className="btn quiet small" onClick={downloadCsv} disabled={!rep}>Download CSV</button>
      </div>
      {error && <p className="error-text" role="alert">{error}</p>}
      {!rep || !t ? <p className="muted">{error ? "" : "Loading…"}</p> : (
        <>
          <div className="kpi-row">
            <Tile label="Booked utilisation" value={pct(t.booked_utilisation)} sub={`${hours(t.booked_min)} of ${hours(t.available_min)} open`} />
            <Tile label="Actual utilisation" value={pct(t.actual_utilisation)} sub={`${hours(t.actual_min)} recorded in room`} />
            <Tile label="Turnover, median" value={mins(t.turnover.median_min)} sub={t.turnover.count ? `average ${mins(t.turnover.avg_min)} over ${t.turnover.count}` : "no back-to-back treatments recorded"} />
            <Tile label="Started on time" value={pct(t.on_time_start)} sub={t.on_time_start.den ? `${t.on_time_start.num} of ${t.on_time_start.den}, within 10 min` : "no starts recorded"} />
            <Tile label="No-shows" value={pct(t.no_show_rate)} sub={`${t.no_show} of ${t.no_show_rate.den} finished bookings`} />
            <Tile label="Bookings" value={String(t.bookings)} sub={`${t.confirmed} confirmed, ${t.tentative} tentative, ${t.cancelled} cancelled`} />
          </div>

          <section className="ot-card" aria-labelledby="rep-rooms">
            <h3 id="rep-rooms">By room</h3>
            <div className="table-wrap">
              <table className="log report-table">
                <thead>
                  <tr><th>Room</th><th>Open</th><th>Booked utilisation</th><th>Actual utilisation</th><th>Turnover (median)</th>
                    <th>On time</th><th>Done</th><th>No-show</th><th>Cancelled</th><th>Overruns</th><th>Overrides</th></tr>
                </thead>
                <tbody>
                  {rep.rooms.map((r) => (
                    <tr key={r.room_id}>
                      <td><strong>{r.name}</strong> <span className="tiny">{r.code}{r.active ? "" : ", inactive"}</span></td>
                      <td>{hours(r.available_min)}</td>
                      <td><Meter rate={r.booked_utilisation} label={`${r.name} booked`} /></td>
                      <td><Meter rate={r.actual_utilisation} label={`${r.name} actual`} /></td>
                      <td>{mins(r.turnover.median_min)}<span className="tiny"> (buffer {r.buffer_minutes})</span></td>
                      <td>{pct(r.on_time_start)}</td>
                      <td>{r.completed}</td><td>{r.no_show}</td><td>{r.cancelled}</td>
                      <td>{r.overruns}{r.avg_overrun_min ? <span className="tiny"> (+{r.avg_overrun_min} min)</span> : ""}</td>
                      <td>{r.overrides}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="tiny">Utilisation = treatment minutes ÷ opening minutes (holidays and room blocks excluded). Actual uses recorded start and finish times; turnover is the gap from one finish to the next start in the same room, ignoring gaps over 2 hours. "—" means nothing to measure yet.</p>
          </section>

          <div className="report-cols">
            <Breakdown title="By treatment" rows={rep.by_procedure.map((x) => [x.procedure, x.bookings, x.minutes])} />
            <Breakdown title="By machine" rows={rep.by_equipment.map((x) => [x.equipment, x.bookings, x.minutes])}
              foot={rep.equipment_downtime_min ? `Machines out for servicing: ${hours(rep.equipment_downtime_min)}.` : undefined} />
            <Breakdown title="By doctor" rows={rep.by_doctor.map((x) => [x.doctor, x.bookings, x.minutes])} />
          </div>
          {!!t.cancel_reasons.length && (
            <section className="ot-card">
              <h3>Cancellation reasons</h3>
              <ul className="plain-list">{t.cancel_reasons.map(([why, n]) => <li key={why}>{why} <span className="tiny">× {n}</span></li>)}</ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function Breakdown({ title, rows, foot }: { title: string; rows: [string, number, number][]; foot?: string }) {
  return (
    <section className="ot-card">
      <h3>{title}</h3>
      {!rows.length ? <p className="tiny">Nothing in this period.</p> : (
        <table className="log">
          <thead><tr><th>{title.replace("By ", "").replace(/^./, (c) => c.toUpperCase())}</th><th>Bookings</th><th>Hours</th></tr></thead>
          <tbody>{rows.map(([n, b, m]) => <tr key={n}><td>{n}</td><td>{b}</td><td>{hours(m)}</td></tr>)}</tbody>
        </table>
      )}
      {foot && <p className="tiny">{foot}</p>}
    </section>
  );
}
