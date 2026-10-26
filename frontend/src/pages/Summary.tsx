import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { Appointments, formatDate, PlanList, TopBar } from "../components/Chrome";
import { api, ApiError } from "../lib/api";
import type { Appointment, PlanItem } from "../lib/types";

interface SummaryData {
  client_first_name: string;
  date: string;
  programme: string;
  session_number: number;
  skin_type: string | null;
  sensitive: boolean;
  findings: { concern: string; label: string; score: number; severity: string }[];
  skin_age: number | null;
  improvement: { previous_date: string; changes: Record<string, { improvement_pct: number; direction: string }> } | null;
  plan: Partial<PlanItem>[];
  warnings: string[];
  approved_by: { name: string; registration_no: string | null } | null;
  approval_status: string | null;
  next_session_date: string | null;
  appointments?: Appointment[];
  disclaimer: string;
}

export function Letter({ s }: { s: SummaryData }) {
  return (
    <article className="letter">
      <header>
        <div>
          <h1>Your skin plan, {s.client_first_name}</h1>
          <p className="muted">{s.programme}, session {s.session_number}. {formatDate(s.date)}.</p>
        </div>
        <span className="approved-stamp">Approved by a doctor</span>
      </header>

      {s.next_session_date && (
        <div className="next"><span className="muted">Next session</span><strong>{formatDate(s.next_session_date)}</strong></div>
      )}

      <section className="section">
        <h2>What we found</h2>
        <p>
          {s.skin_type ? `Your skin type is ${s.skin_type}${s.sensitive ? " and sensitive" : ""}. ` : ""}
          The analysis showed {s.findings.map((f) => `${f.severity} ${f.label}`).join(", ")}.
          {s.skin_age ? ` Estimated skin age ${s.skin_age}.` : ""}
        </p>
        {s.improvement && (
          <p className="muted">
            Since {formatDate(s.improvement.previous_date)}:{" "}
            {Object.entries(s.improvement.changes).map(([c, ch]) =>
              `${c.replace("_", " ")} ${ch.direction === "better" ? `${Math.round(ch.improvement_pct)}% better` : ch.direction}`).join(", ")}.
          </p>
        )}
      </section>

      {!!s.appointments?.filter((a) => a.status !== "cancelled").length && (
        <section className="section">
          <h2>Your appointments</h2>
          <Appointments items={s.appointments} />
        </section>
      )}

      <section className="section">
        <h2>Your plan</h2>
        <PlanList items={s.plan} />
        {!!s.warnings.length && <ul className="note-list">{s.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      </section>

      <footer>
        {s.approved_by && <p>Approved by {s.approved_by.name}{s.approved_by.registration_no ? `, Reg. No. ${s.approved_by.registration_no}` : ""}.</p>}
        <p>{s.disclaimer}</p>
      </footer>
      <div className="no-print"><button className="btn quiet" onClick={() => window.print()}>Print or save as PDF</button></div>
    </article>
  );
}

export function VisitSummary() {
  const { id } = useParams<{ id: string }>();
  const from = (useLocation().state as { from?: string } | null)?.from;
  const [s, setS] = useState<SummaryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<SummaryData>(`/visits/${id}/summary`).then(setS).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the summary."));
  }, [id]);
  return (
    <>
      <TopBar />
      <main className="summary-page">
        {from && <div className="no-print summary-back"><Link className="btn quiet small" to={from}>‹ Back to the conversation</Link></div>}
        {s ? <Letter s={s} /> : <p className={error ? "error-text" : "muted"}>{error ?? "Loading…"}</p>}
      </main>
    </>
  );
}

// A past visit's summary, opened from the History tab on the clinic device (scoped to the verified client).
export function PastSummary() {
  const { id, visitId } = useParams<{ id: string; visitId: string }>();
  const [s, setS] = useState<SummaryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<SummaryData>(`/conversations/${id}/visits/${visitId}/summary`).then(setS)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load that summary."));
  }, [id, visitId]);
  return (
    <>
      <TopBar />
      <main className="summary-page">
        <div className="no-print summary-back"><Link className="btn quiet small" to={`/session/${id}`}>‹ Back to the conversation</Link></div>
        {s ? <Letter s={s} /> : <p className={error ? "error-text" : "muted"}>{error ?? "Loading…"}</p>}
      </main>
    </>
  );
}

export function PublicSummary() {
  const { token } = useParams<{ token: string }>();
  const [s, setS] = useState<SummaryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<SummaryData>(`/public/summaries/${token}`, { auth: false }).then(setS)
      .catch((e) => setError(e instanceof ApiError ? e.message : "This link isn't available."));
  }, [token]);
  return <main className="summary-page">{s ? <Letter s={s} /> : <p className={error ? "error-text" : "muted"}>{error ?? "Loading…"}</p>}</main>;
}
