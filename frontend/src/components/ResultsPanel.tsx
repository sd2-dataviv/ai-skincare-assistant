import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Finding, ImageSource, ModelImageClient, PlanItem } from "../lib/types";
import { AuthedImage, Findings, formatDate, PlanList } from "./Chrome";
import { ClientModelPictures } from "./ModelPictures";

interface Results {
  visit_id: string;
  stage: string;
  photos: { id: string; pose: string }[];
  findings: Finding[];
  confidence: number | null;
  engine: string | null;
  anti_aging: { skin_age?: number | null; firmness?: number } | null;
  comparison: { previous_date: string; changes: Record<string, { before: number; after: number; improvement_pct: number; direction: string }> } | null;
  skin_type: string | null;
  sensitive: boolean;
  concerns: string[];
  plan: { status: string; items?: PlanItem[]; warnings?: string[]; excluded?: { name: string; reason: string }[]; hidden?: boolean } | null;
  images?: ModelImageClient[];  // only once the plan is approved
}

const deviceSrc: ImageSource = (id, variant = "image") => ({ path: `/model-images/${id}/file${variant === "before" ? "?variant=before" : ""}` });

export default function ResultsPanel({ conversationId, version }: { conversationId: string; version: number }) {
  const [r, setR] = useState<Results | null>(null);
  useEffect(() => {
    api<Results>(`/conversations/${conversationId}/results`).then(setR).catch(() => undefined);
  }, [conversationId, version]);

  if (!r) return <div className="results"><p className="muted">Loading results…</p></div>;
  const approved = r.plan?.status === "approved";
  return (
    <div className="results">
      <div className="thumbs">
        {r.photos.slice(-3).map((p) => (
          <figure key={p.id}><AuthedImage photoId={p.id} alt={`${p.pose} photo`} /><figcaption>{p.pose}</figcaption></figure>
        ))}
      </div>

      <div className="facts">
        {r.skin_type && <div className="fact"><strong style={{ textTransform: "capitalize" }}>{r.skin_type}</strong><span>Skin type{r.sensitive ? ", sensitive" : ""}</span></div>}
        {r.anti_aging?.skin_age != null && <div className="fact"><strong>{r.anti_aging.skin_age}</strong><span>Estimated skin age</span></div>}
      </div>

      <section className="section" aria-labelledby="f-h">
        <h2 id="f-h">What the analysis found</h2>
        <Findings findings={r.findings} raised={r.concerns} />
        <p className="tiny">Scores are out of 100; lower is better. These are AI observations for the doctor to confirm.
          {r.engine?.startsWith("mock") ? " (Demo analysis engine.)" : ""}</p>
      </section>

      {!!r.images?.length && (
        <section className="section" aria-labelledby="mp-c-h">
          <h2 id="mp-c-h">Your photos</h2>
          <ClientModelPictures images={r.images} src={deviceSrc} />
        </section>
      )}

      {r.comparison && (
        <section className="section" aria-labelledby="c-h">
          <h2 id="c-h">Since {formatDate(r.comparison.previous_date)}</h2>
          <ul className="note-list">
            {Object.entries(r.comparison.changes).map(([c, ch]) => (
              <li key={c} style={{ textTransform: "none" }}>
                {c.replace("_", " ")}: {Math.round(ch.before)} → {Math.round(ch.after)}{" "}
                ({ch.direction === "better" ? `${Math.round(ch.improvement_pct)}% better` : ch.direction === "worse" ? `${Math.abs(Math.round(ch.improvement_pct))}% worse` : "stable"})
              </li>
            ))}
          </ul>
        </section>
      )}

      {r.plan && (
        <section className="section" aria-labelledby="p-h">
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
            <h2 id="p-h">{approved ? "Your plan" : "Draft plan"}</h2>
            {approved ? <span className="approved-stamp">Doctor approved</span> : <span className="draft-stamp">Draft — awaiting doctor</span>}
          </div>
          {r.plan.hidden ? (
            <p className="muted">Your doctor will review the plan before you see it.</p>
          ) : (
            <>
              <PlanList items={r.plan.items ?? []} />
              {!!r.plan.excluded?.length && (
                <p className="tiny">Left out for safety: {r.plan.excluded.map((e) => `${e.name} (${e.reason})`).join("; ")}.</p>
              )}
              {!!r.plan.warnings?.length && <ul className="note-list">{r.plan.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
            </>
          )}
        </section>
      )}
    </div>
  );
}
