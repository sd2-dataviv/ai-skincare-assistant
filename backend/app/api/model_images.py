"""Pictures from the skin models. Doctors and admins see everything; everyone else only what the client may see
(after approval). Unknown or not-allowed pictures answer 404 alike."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import audit, get_or_404
from app.db import get_db
from app.models import Client, Photo, SummaryShare, User, Visit
from app.security import current_client, doctor_or_admin, staff
from app.services import DomainError
from app.services import model_images as mi
from app.services.photos import accepted_by_pose

router = APIRouter(tags=["skin model images"])

Variant = Query("image", pattern="^(image|before)$")


def _image(data: bytes, content_type: str) -> Response:
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "no-store"})


@router.get("/visits/{visit_id}/model-images")
async def list_images(visit_id: uuid.UUID, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    await get_or_404(db, Visit, visit_id)
    return {"images": [mi.staff_view(r) for r in await mi.for_visit(db, visit_id)]}


@router.post("/visits/{visit_id}/model-images/preview", status_code=202)
async def retry_preview(visit_id: uuid.UUID, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Make the post-treatment preview again (e.g. after the model server was down)."""
    visit = await get_or_404(db, Visit, visit_id)
    photos = (await db.execute(select(Photo).where(Photo.visit_id == visit.id).order_by(Photo.created_at))).scalars().all()
    front = accepted_by_pose(list(photos)).get("front")
    if front is None:
        raise DomainError("photos_missing", "This visit has no accepted front photo.", 409)
    row = await mi.start_preview(db, visit, front)
    if row is None:
        raise DomainError("not_configured", "No post-treatment model is set up (SKIN_POST_TREATMENT_WS_URL).", 409)
    await audit(db, user, "make_preview", visit.id, {}, {"image_id": str(row.id)}, "Staff asked for the preview again")
    await db.commit()
    return mi.staff_view(row)


@router.get("/model-images/{image_id}/file")
async def staff_file(image_id: uuid.UUID, variant: str = Variant, user: User = Depends(staff),
                     db: AsyncSession = Depends(get_db)):
    # Operators run the clinic device the client looks at, so they get the client's rules.
    data, ctype = await mi.read_file(db, image_id, for_client=user.role == "operator", variant=variant)
    return _image(data, ctype)


@router.get("/me/model-images/{image_id}/file")
async def client_file(image_id: uuid.UUID, variant: str = Variant, client: Client = Depends(current_client),
                      db: AsyncSession = Depends(get_db)):
    data, ctype = await mi.read_file(db, image_id, for_client=True, variant=variant, client_id=client.id)
    return _image(data, ctype)


@router.get("/public/summaries/{token}/images/{image_id}")
async def shared_file(token: str, image_id: uuid.UUID, variant: str = Variant, db: AsyncSession = Depends(get_db)):
    share = (await db.execute(select(SummaryShare).where(SummaryShare.token == token))).scalar_one_or_none()
    if share is None or share.expires_at < datetime.now(timezone.utc):
        raise HTTPException(404, "This link has expired or is invalid.")
    data, ctype = await mi.read_file(db, image_id, for_client=True, variant=variant, visit_id=share.visit_id)
    return _image(data, ctype)
