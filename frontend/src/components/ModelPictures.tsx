import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { ImageSource, ModelImageClient, ModelImageStaff } from "../lib/types";
import { BlobImage } from "./Chrome";

const CLASS_WORD: Record<string, string> = { acne: "acne", pigmentation: "pigmentation", "dark circles": "dark circles", scars: "scars" };

function foundText(found: Record<string, number>) {
  const parts = Object.entries(found).map(([c, n]) => `${n} ${CLASS_WORD[c] ?? c}`);
  return parts.length ? parts.join(", ") : "nothing";
}

export interface ModelStatus {
  status: "ok" | "partial" | "no_findings" | "no_face" | "unavailable" | "off";
  message?: string | null;
  extras?: Record<string, { count: number }>;
}

const staffSrc: ImageSource = (id, variant = "image") => ({ path: `/model-images/${id}/file${variant === "before" ? "?variant=before" : ""}` });

// Doctor's review: what the skin models made of the photos, the preview (with its status), and a retry when the
// preview failed. A note says the client sees these only after approval.
export function StaffModelPictures({ visitId, images, model, approved, onReload }: {
  visitId: string;
  images: ModelImageStaff[];
  model: ModelStatus | null;
  approved: boolean;
  onReload: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const overlays = images.filter((i) => i.kind === "diagnosis_overlay");
  const preview = images.find((i) => i.kind === "post_treatment_preview");

  // A preview being made: check again every few seconds.
  useEffect(() => {
    if (preview?.status !== "pending") return;
    const t = setInterval(onReload, 5000);
    return () => clearInterval(t);
  }, [preview?.status, onReload]);

  if (!overlays.length && !preview && (!model || model.status === "off")) return null;

  async function retry() {
    setBusy(true);
    setError(null);
    try {
      await api(`/visits/${visitId}/model-images/preview`, { method: "POST" });
      onReload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't ask for the preview.");
    } finally {
      setBusy(false);
    }
  }

  const extras = Object.entries(model?.extras ?? {}).filter(([, v]) => v && v.count > 0);
  return (
    <section className="section model-pics" aria-labelledby="mp-h">
      <h2 id="mp-h">Skin model</h2>
      {model && model.status !== "ok" && model.message && (
        <p className={`tiny model-note ${model.status === "unavailable" || model.status === "no_face" ? "warn" : ""}`}>{model.message}</p>
      )}
      {extras.length > 0 && (
        <p className="tiny">Also found (not scored): {extras.map(([c, v]) => `${v.count} ${c}`).join(", ")}.</p>
      )}
      {overlays[0]?.warning && <p className="tiny model-note warn">{overlays[0].warning}</p>}
      {overlays.length > 0 && (
        <div className="mp-grid">
          {overlays.map((o) => (
            <figure key={o.id}>
              <BlobImage {...staffSrc(o.id)} alt={`${o.pose} photo with the model's findings marked`} />
              <figcaption>
                <strong style={{ textTransform: "capitalize" }}>{o.pose}</strong>
                <span>{o.face_detected === false ? "No face found" : o.findings_sent ? `Found ${foundText(o.found)}` : "Marked-up photo"}</span>
              </figcaption>
            </figure>
          ))}
        </div>
      )}
      {preview && (
        <div className="mp-preview">
          <div className="mp-preview-head">
            <h3>Post-treatment preview</h3>
            <span className="pill quiet">
              {preview.status === "ready" && (preview.warning || preview.changed === false) ? "Not shown to the client"
                : approved ? "Client can see this" : "Client sees this after approval"}
            </span>
          </div>
          {preview.status === "pending" && <p className="muted" role="status">Making the preview… this can take a few minutes.</p>}
          {preview.status === "failed" && (
            <div className="mp-failed">
              <p className="error-text">{preview.error ?? "The preview couldn't be made."}</p>
              <button className="btn quiet small" onClick={retry} disabled={busy}>{busy ? "Asking…" : "Try again"}</button>
            </div>
          )}
          {preview.status === "ready" && (
            <>
              {preview.warning && <p className="tiny model-note warn">{preview.warning}</p>}
              <BeforeAfter image={preview} src={staffSrc} />
              {preview.changed === false && <p className="tiny">The model found nothing to paint out, so the client won't be shown this preview.</p>}
              <p className="tiny">{preview.caption}</p>
              <button className="linkish" onClick={retry} disabled={busy}>Make it again</button>
            </>
          )}
          {error && <p className="error-text" role="alert">{error}</p>}
        </div>
      )}
    </section>
  );
}

function BeforeAfter({ image, src }: { image: { id: string; has_before: boolean }; src: ImageSource }) {
  return (
    <div className={`ba${image.has_before ? "" : " single"}`}>
      {image.has_before && (
        <figure><BlobImage {...src(image.id, "before")} alt="Before: the face as photographed" /><figcaption>Now</figcaption></figure>
      )}
      <figure><BlobImage {...src(image.id)} alt="Illustration: the marked spots painted out" /><figcaption>Illustration</figcaption></figure>
    </div>
  );
}

// What the client sees once the plan is approved: the marked-up photos and the preview, always as an illustration.
export function ClientModelPictures({ images, src }: { images: ModelImageClient[]; src: ImageSource }) {
  if (!images.length) return null;
  const overlays = images.filter((i) => i.kind === "diagnosis_overlay");
  const preview = images.find((i) => i.kind === "post_treatment_preview");
  return (
    <div className="model-pics client">
      {overlays.length > 0 && (
        <>
          <div className="mp-grid">
            {overlays.map((o) => (
              <figure key={o.id}><BlobImage {...src(o.id)} alt={`${o.title}, with the marked areas`} /><figcaption>{o.title}</figcaption></figure>
            ))}
          </div>
          <p className="tiny">{overlays[0].caption}</p>
        </>
      )}
      {preview && (
        <div className="mp-preview">
          <div className="mp-preview-head">
            <h3>{preview.title}</h3>
            <span className="illustration-tag">Illustration only</span>
          </div>
          <BeforeAfter image={preview} src={src} />
          <p className="tiny">{preview.caption}</p>
        </div>
      )}
    </div>
  );
}
