"""Diagnosis, anti-ageing and improvement comparison (post-treatment / measure improvement)."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.analysis_engine import get_analysis_engine
from app.integrations.storage import get_storage
from app.models import Analysis, Client, Photo, Visit
from app.services import DomainError
from app.services.clients import age_of
from app.services.photos import accepted_by_pose, missing_poses

CONCERN_LABELS = {
    "acne": "acne", "pigmentation": "pigmentation and uneven tone", "wrinkles": "fine lines and wrinkles",
    "dark_circles": "dark circles", "redness": "redness", "pores": "visible pores", "dullness": "dullness",
    "dryness": "dryness",
}


def latest(analyses: list[Analysis], kind: str) -> Analysis | None:
    found = [a for a in analyses if a.kind == kind]
    return found[-1] if found else None


async def _previous_diagnosis(db: AsyncSession, visit: Visit) -> tuple[Visit, Analysis] | None:
    row = (
        await db.execute(
            select(Visit, Analysis)
            .join(Analysis, Analysis.visit_id == Visit.id)
            .where(Visit.client_id == visit.client_id, Visit.id != visit.id, Visit.created_at < visit.created_at,
                   Analysis.kind == "diagnosis")
            .order_by(Visit.created_at.desc(), Analysis.created_at.desc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


def compare(before: dict, after: dict, concerns: list[str]) -> dict:
    """Per-concern change. Lower scores are better; positive improvement_pct means better."""
    focus = [c for c in concerns if c in before and c in after] or [
        c for c in after if c in before and before[c]["score"] >= 40
    ]
    out = {}
    for c in focus:
        b, a = before[c]["score"], after[c]["score"]
        pct = round((b - a) / b * 100, 1) if b else 0.0
        out[c] = {"before": b, "after": a, "improvement_pct": pct,
                  "direction": "better" if pct >= 5 else "worse" if pct <= -5 else "stable"}
    return out


async def run_analysis(db: AsyncSession, visit: Visit, client: Client, photos: list[Photo]) -> dict:
    missing = missing_poses(photos)
    if missing:
        raise DomainError("photos_missing", f"Still need photos: {', '.join(missing)}.")
    by_pose = accepted_by_pose(photos)
    storage = get_storage()
    images = {pose: await storage.get(p.storage_key) for pose, p in by_pose.items()}
    result = await get_analysis_engine().analyse(images, age_of(client))

    front = by_pose["front"]
    diag = Analysis(visit_id=visit.id, photo_id=front.id, kind="diagnosis", engine=result.engine,
                    result={"diagnosis": result.diagnosis, "flags": result.flags}, confidence=result.confidence)
    aa = Analysis(visit_id=visit.id, photo_id=front.id, kind="anti_aging", engine=result.engine,
                  result=result.anti_aging, confidence=result.confidence)
    db.add_all([diag, aa])
    out = {"diagnosis": result.diagnosis, "anti_aging": result.anti_aging, "confidence": result.confidence,
           "flags": result.flags, "engine": result.engine, "comparison": None}

    prev = await _previous_diagnosis(db, visit)
    if prev:
        prev_visit, prev_diag = prev
        cmp = compare(prev_diag.result["diagnosis"], result.diagnosis, list(visit.concerns or []))
        if cmp:
            db.add(Analysis(visit_id=visit.id, kind="compare", engine="compare-v1", confidence=result.confidence,
                            result={"previous_visit_id": str(prev_visit.id),
                                    "previous_date": prev_visit.created_at.date().isoformat(), "changes": cmp}))
            out["comparison"] = {"previous_date": prev_visit.created_at.date().isoformat(), "changes": cmp}
    await db.flush()
    return out


def top_findings(diagnosis: dict, concerns: list[str], n: int = 3, threshold: float = 40) -> list[dict]:
    items = [{"concern": c, **v} for c, v in diagnosis.items()]
    items.sort(key=lambda x: (x["concern"] not in concerns, -x["score"]))
    picked = [i for i in items if i["score"] >= threshold or i["concern"] in concerns][:n]
    return picked or items[:1]
