from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.knowledge import questionnaire
from app.models import CatalogueItem, SummaryShare, User, Visit
from app.schemas import CatalogueOut
from app.security import staff
from app.services.summary import build_summary

router = APIRouter(tags=["catalogue"])
public = APIRouter(prefix="/public", tags=["public"])


@router.get("/catalogue", response_model=list[CatalogueOut])
async def list_catalogue(_: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(CatalogueItem).where(CatalogueItem.active.is_(True)).order_by(CatalogueItem.kind, CatalogueItem.name))).scalars().all()


@router.get("/knowledge/questionnaire")
async def get_questionnaire(_: User = Depends(staff)):
    return questionnaire()


@public.get("/summaries/{token}")
async def public_summary(token: str, db: AsyncSession = Depends(get_db)):
    """Token-gated, expiring, read-only summary for the client (approved plans only)."""
    share = (await db.execute(select(SummaryShare).where(SummaryShare.token == token))).scalar_one_or_none()
    if share is None or share.expires_at < datetime.now(timezone.utc):
        raise HTTPException(404, "This link has expired or is invalid.")
    visit = await db.get(Visit, share.visit_id)
    return await build_summary(db, visit)
