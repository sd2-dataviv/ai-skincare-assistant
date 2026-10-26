import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { fetchBlobUrl } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Appointment, Finding, PlanItem, Stage } from "../lib/types";

export function TopBar({ children }: { children?: ReactNode }) {
  const { user, logout } = useAuth();
  return (
    <header className="topbar">
      <Link to="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
        <span className="brand-mark" aria-hidden /> Skin consultation
      </Link>
      {children}
      <span className="spacer" />
      {user && !children && user.role !== "operator" && (
        <nav className="topnav" aria-label="Sections">
          <NavLink to="/doctor">Review queue</NavLink>
          <NavLink to="/ot">Treatment rooms</NavLink>
        </nav>
      )}
      {user && (
        <>
          <span className="who">{user.full_name}</span>
          <button className="btn quiet small" onClick={logout}>Sign out</button>
        </>
      )}
    </header>
  );
}

const RAIL: { stage: Stage; label: string }[] = [
  { stage: "identify", label: "Identify" },
  { stage: "concern", label: "Concern" },
  { stage: "assess", label: "Know your skin" },
  { stage: "capture", label: "Photos" },
  { stage: "analyse", label: "Analysis" },
  { stage: "recommend", label: "Plan" },
  { stage: "approval", label: "Doctor review" },
  { stage: "booking", label: "Book treatment" },
  { stage: "wrap_up", label: "Wrap up" },
];

export function StageRail({ step }: { step: number | null }) {
  const current = step ?? 0;
  return (
    <ol className="rail" aria-label="Consultation steps">
      {RAIL.map((r, i) => {
        const n = i + 1;
        const cls = n < current || current > RAIL.length ? "done" : n === current ? "now" : "";
        return (
          <li key={r.stage} className={cls} aria-current={n === current ? "step" : undefined}>
            <span className="n">{n}</span>
            <span className="label">{r.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function AuthedImage({ photoId, alt }: { photoId: string; alt: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let url: string | null = null;
    let alive = true;
    fetchBlobUrl(`/photos/${photoId}/file`).then((u) => {
      url = u;
      if (alive) setSrc(u);
    }).catch(() => undefined);
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [photoId]);
  return src ? <img src={src} alt={alt} /> : <img alt={alt} />;
}

const SEVERITY: [number, string][] = [[25, "minimal"], [45, "mild"], [70, "moderate"], [101, "severe"]];
export const severityOf = (score: number) => SEVERITY.find(([max]) => score < max)![1];

export function Findings({ findings, raised = [] }: { findings: Finding[]; raised?: string[] }) {
  return (
    <div className="findings">
      {findings.map((f) => {
        const sev = severityOf(f.score);
        return (
          <div key={f.concern} className={`finding${raised.includes(f.concern) ? " raised" : ""}`}>
            <span className="name">{f.label.charAt(0).toUpperCase() + f.label.slice(1)}</span>
            <span className="bar" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(f.score)}
              aria-label={`${f.label}: ${sev}`}>
              <i className={sev} style={{ width: `${f.score}%` }} />
            </span>
            <span className="score">{Math.round(f.score)}</span>
          </div>
        );
      })}
    </div>
  );
}

const KIND: Record<string, string> = { service: "Clinic treatment", product: "Home care", doctor_service: "Doctor service" };

export function PlanList({ items }: { items: Partial<PlanItem>[] }) {
  return (
    <div className="plan">
      {items.map((i, idx) => (
        <div className="plan-item" key={`${i.name}-${idx}`}>
          <div>
            <div className="kind">{KIND[i.kind ?? ""] ?? i.kind}</div>
            <strong>{i.name}</strong>
          </div>
          <div className="qty">{i.sessions ? `${i.sessions} session${i.sessions > 1 ? "s" : ""}` : i.instructions ?? ""}</div>
          {i.reason && <div className="why">{i.reason}{i.sessions && i.instructions ? ` ${i.instructions}` : ""}</div>}
        </div>
      ))}
    </div>
  );
}

export function Appointments({ items }: { items: Partial<Appointment>[] }) {
  const live = items.filter((a) => a.status !== "cancelled");
  if (!live.length) return null;
  return (
    <ul className="appts">
      {live.map((a, i) => (
        <li key={`${a.procedure}-${i}`} className={a.status}>
          <span className="when">{(a.status === "confirmed" || a.status === "tentative") && a.label ? a.label : "To be booked"}
            {a.status === "tentative" && <span className="tiny"> · to be confirmed</span>}</span>
          <span className="what"><strong>{a.procedure}</strong>
            {a.sessions_total && a.sessions_total > 1 ? `, session ${a.session_index} of ${a.sessions_total}` : ""}</span>
          <span className="where">{a.status === "confirmed" || a.status === "tentative" ? a.room_name : "The front desk will call you"}</span>
        </li>
      ))}
    </ul>
  );
}

export function formatDate(iso: string | null | undefined) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
}
