"""Pictures from the clinic's skin models: diagnosis overlays (made during the analysis) and the post-treatment
preview (made in the background, because inpainting can take minutes on a CPU).

Who sees what:
- doctors and admins: everything, any time, including pending and failed previews and the model's findings;
- the client (their phone, the clinic device, a shared summary link): only once the plan is approved, only finished
  pictures, and the preview only when the model actually changed something. The preview is always captioned as an
  illustration, not a promised result.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import sessionmaker
from app.integrations import skin_models as sm
from app.integrations.analysis_engine import EngineImage
from app.integrations.storage import get_storage
from app.models import ModelImage, Photo, Prescription, Visit
from app.services import DomainError

log = logging.getLogger(__name__)

OVERLAY = "diagnosis_overlay"
PREVIEW = "post_treatment_preview"
PREVIEW_CAPTION = ("Illustration only: a computer-made picture of the marked spots painted out. It is not a promise of "
                   "results; how your skin responds depends on your skin and on the treatment.")
POSE_WORD = {"front": "front", "left": "left side", "right": "right side"}
SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg"}

_tasks: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def save_overlays(db: AsyncSession, visit: Visit, photos_by_pose: dict[str, Photo], images: list[EngineImage]) -> None:
    """Keep the latest analysis's overlays (an analysis run again replaces them)."""
    if not images:
        return
    await db.execute(delete(ModelImage).where(ModelImage.visit_id == visit.id, ModelImage.kind == OVERLAY))
    storage = get_storage()
    for im in images:
        key = await storage.put(im.data, SUFFIX.get(im.content_type, ".jpg"))
        out = cv2.imdecode(np.frombuffer(im.data, np.uint8), cv2.IMREAD_COLOR)
        if out is not None:
            im.details = {**im.details, "size": [out.shape[1], out.shape[0]], "findings_sent": "detections" in im.details}
        photo = photos_by_pose.get(im.pose)
        db.add(ModelImage(visit_id=visit.id, photo_id=photo.id if photo else None, kind=im.kind, pose=im.pose,
                          status="ready", model=im.model[:60], storage_key=key, content_type=im.content_type,
                          details=im.details))
    await db.flush()


async def start_preview(db: AsyncSession, visit: Visit, front: Photo, front_bytes: bytes | None = None) -> ModelImage | None:
    """Queue the post-treatment preview for the front photo. Returns None when no preview model is configured.
    The row is created now (status pending); the picture is filled in by a background task once the model answers."""
    svc = sm.post_treatment_service()
    if svc is None:
        return None
    await db.execute(delete(ModelImage).where(ModelImage.visit_id == visit.id, ModelImage.kind == PREVIEW))
    row = ModelImage(visit_id=visit.id, photo_id=front.id, kind=PREVIEW, pose="front", status="pending",
                     model=svc.name[:60], details={})
    db.add(row)
    await db.flush()
    data = front_bytes if front_bytes is not None else await get_storage().get(front.storage_key)
    task = asyncio.create_task(_make_preview(row.id, data, svc))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return row


async def drain() -> None:
    """Wait for background previews (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def _before_crop(front: bytes, details: dict | None, out_size: tuple[int, int] | None) -> bytes | None:
    """The same face crop the model worked on, untreated, so the preview can be shown side by side."""
    img = cv2.imdecode(np.frombuffer(front, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    box = (details or {}).get("face_box")
    if box and len(box) == 4:
        x1, y1, x2, y2 = (int(v) for v in box)
        crop = img[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
        if crop.size:
            img = crop
    if out_size:
        img = cv2.resize(img, out_size)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None


async def _make_preview(image_id: uuid.UUID, front: bytes, svc: sm.ServiceConfig) -> None:
    update: dict
    try:
        reply = await sm.call_service(svc, front)
        out = cv2.imdecode(np.frombuffer(reply.image, np.uint8), cv2.IMREAD_COLOR)
        size = (out.shape[1], out.shape[0]) if out is not None else None
        storage = get_storage()
        key = await storage.put(reply.image, SUFFIX.get(reply.content_type, ".jpg"))
        before = _before_crop(front, reply.details, size)
        before_key = await storage.put(before, ".jpg") if before else None
        d = reply.details or {}
        want = svc.inner_params().get("output_image_size")
        mismatch = bool(want and size and list(size) != [int(v) for v in want][:2])
        update = {"status": "ready", "storage_key": key, "content_type": reply.content_type, "before_storage_key": before_key,
                  "error": None,
                  "details": {**d, "seconds": reply.seconds, "findings_sent": reply.details is not None,
                              "changed": bool(d.get("inpainted")) if reply.details is not None else None,
                              "size": list(size) if size else None, "size_mismatch": mismatch}}
        if reply.details is not None and not d.get("face_detected", True):
            update["status"], update["error"] = "failed", "The model did not find a face in the front photo."
    except sm.ModelUnavailable as e:
        update = {"status": "failed", "error": str(e)}
    except Exception as e:  # noqa: BLE001 - a background job must never take the app down
        log.exception("post-treatment preview failed")
        update = {"status": "failed", "error": f"Unexpected error: {type(e).__name__}"}

    # The request that queued us commits the pending row; wait for it if we're faster than that commit.
    for _ in range(150):
        async with sessionmaker()() as db:
            row = await db.get(ModelImage, image_id)
            if row is not None:
                for k, v in update.items():
                    setattr(row, k, v)
                await db.commit()
                return
        await asyncio.sleep(0.2)
    log.info("preview %s dropped: its request was rolled back", image_id)


def _stale(row: ModelImage) -> bool:
    s = get_settings()
    limit = s.skin_post_treatment_timeout_s + s.skin_model_connect_timeout_s + 120
    return row.status == "pending" and row.created_at < _now() - timedelta(seconds=limit)


def _counts(details: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in (details or {}).get("detections") or []:
        out[x.get("class", "?")] = out.get(x.get("class", "?"), 0) + 1
    return out


def _warning(row: ModelImage) -> str | None:
    d = row.details or {}
    if row.status != "ready":
        return None
    if not d.get("findings_sent"):
        size = d.get("size")
        odd = f" It sent a {size[0]}×{size[1]} picture" if row.kind == PREVIEW and d.get("size_mismatch") and size else ""
        return ("This service sent a picture but not its findings, so the app can't confirm it is the right model."
                + (odd + ", not the size asked for." if odd else "") + " The client won't see it.")
    if row.kind == PREVIEW and d.get("size_mismatch"):
        return "The picture is not the size asked for; check that this address runs the post-treatment model."
    return None


def staff_view(row: ModelImage) -> dict:
    status, error = row.status, row.error
    if _stale(row):
        status, error = "failed", "The preview took too long (the app may have restarted). Try again."
    d = row.details or {}
    return {"id": str(row.id), "kind": row.kind, "pose": row.pose, "status": status, "error": error, "model": row.model,
            "has_before": bool(row.before_storage_key), "found": _counts(d), "face_detected": d.get("face_detected"),
            "findings_sent": bool(d.get("findings_sent")), "warning": _warning(row),
            "changed": d.get("changed"), "seconds": d.get("seconds"),
            "caption": PREVIEW_CAPTION if row.kind == PREVIEW else
            f"{POSE_WORD.get(row.pose, row.pose).capitalize()} photo with what the model marked",
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def client_visible(row: ModelImage) -> bool:
    """Only pictures from a service that also reported what it found (so we know it is the model we asked for and
    that it found something); a preview only when it actually painted something out."""
    d = row.details or {}
    if row.status != "ready" or not row.storage_key or not d.get("findings_sent") or d.get("face_detected") is False:
        return False
    if row.kind == PREVIEW:
        return d.get("changed") is True and not d.get("size_mismatch")
    return True


def client_view(row: ModelImage) -> dict:
    if row.kind == PREVIEW:
        return {"id": str(row.id), "kind": row.kind, "pose": row.pose, "has_before": bool(row.before_storage_key),
                "title": "Illustration after treatment", "caption": PREVIEW_CAPTION}
    return {"id": str(row.id), "kind": row.kind, "pose": row.pose, "has_before": False,
            "title": f"Your {POSE_WORD.get(row.pose, row.pose)} photo",
            "caption": "The areas the skin analysis marked. Your doctor has reviewed these findings."}


async def for_visit(db: AsyncSession, visit_id: uuid.UUID) -> list[ModelImage]:
    order = {OVERLAY: 0, PREVIEW: 1}
    pose = {"front": 0, "left": 1, "right": 2}
    rows = (await db.execute(select(ModelImage).where(ModelImage.visit_id == visit_id))).scalars().all()
    return sorted(rows, key=lambda r: (order.get(r.kind, 9), pose.get(r.pose, 9)))


async def plan_approved(db: AsyncSession, visit_id: uuid.UUID) -> bool:
    rx = (await db.execute(select(Prescription).where(Prescription.visit_id == visit_id))).scalar_one_or_none()
    return rx is not None and rx.status == "approved"


async def client_list(db: AsyncSession, visit_id: uuid.UUID) -> list[dict]:
    """What the client may see for this visit: nothing before approval."""
    if not await plan_approved(db, visit_id):
        return []
    return [client_view(r) for r in await for_visit(db, visit_id) if client_visible(r)]


async def read_file(db: AsyncSession, image_id: uuid.UUID, *, for_client: bool, variant: str = "image",
                    visit_id: uuid.UUID | None = None, client_id: uuid.UUID | None = None) -> tuple[bytes, str]:
    """The picture's bytes, after the visibility rules. Anything not allowed answers 404 (no hint that it exists)."""
    row = await db.get(ModelImage, image_id)
    missing = DomainError("not_found", "Image not found.", 404)
    if row is None or (visit_id is not None and row.visit_id != visit_id):
        raise missing
    if client_id is not None:
        visit = await db.get(Visit, row.visit_id)
        if visit is None or visit.client_id != client_id:
            raise missing
    if for_client and (not client_visible(row) or not await plan_approved(db, row.visit_id)):
        raise missing
    key = row.before_storage_key if variant == "before" else row.storage_key
    if not key:
        raise missing
    return await get_storage().get(key), ("image/jpeg" if variant == "before" else row.content_type)
