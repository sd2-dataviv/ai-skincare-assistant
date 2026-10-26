"""Client and visit-start endpoints (spec §06: find_or_import_client, start_visit, add_note)."""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import audit, get_or_404
from app.db import get_db
from app.idempotency import run_idempotent
from app.models import Client, Note, User, Visit
from app.schemas import ClientCreateIn, ClientImportIn, ClientOut, NoteIn, NoteOut, VisitCreateIn, VisitOut
from app.security import staff
from app.services.clients import find_or_import, normalise_mobile
from app.services.summary import add_note
from app.services.visits import start_visit

router = APIRouter(tags=["clients"])


@router.get("/clients", response_model=list[ClientOut])
async def search_clients(mobile: str | None = None, q: str | None = Query(default=None, max_length=100),
                         _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    stmt = select(Client)
    if mobile:
        stmt = stmt.where(Client.mobile == normalise_mobile(mobile))
    elif q:
        stmt = stmt.where(or_(Client.full_name.ilike(f"%{q}%"), Client.mobile.like(f"%{q}%")))
    return (await db.execute(stmt.order_by(Client.created_at.desc()).limit(50))).scalars().all()


@router.get("/clients/{client_id}", response_model=ClientOut)
async def get_client(client_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    return await get_or_404(db, Client, client_id)


@router.post("/clients/import")
async def import_client(body: ClientImportIn, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    client, how = await find_or_import(db, body.mobile)
    await audit(db, user, "find_or_import_client", None, {"mobile": "(redacted)"}, {"status": how}, "Staff lookup")
    await db.commit()
    return {"status": how, "client": ClientOut.model_validate(client).model_dump(mode="json") if client else None}


@router.post("/clients", response_model=ClientOut, status_code=201)
async def create_client(body: ClientCreateIn, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    client, how = await find_or_import(db, body.mobile, body.full_name, body.dob, body.gender)
    await audit(db, user, "find_or_import_client", None, {"mobile": "(redacted)"}, {"status": how}, "Staff created client")
    await db.commit()
    return client


@router.get("/clients/{client_id}/visits", response_model=list[VisitOut])
async def client_visits(client_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Visit).where(Visit.client_id == client_id).order_by(Visit.created_at.desc()))).scalars().all()


@router.post("/clients/{client_id}/visits", response_model=VisitOut, status_code=201)
async def create_visit(client_id: uuid.UUID, body: VisitCreateIn, request: Request, user: User = Depends(staff),
                       db: AsyncSession = Depends(get_db)):
    client = await get_or_404(db, Client, client_id)

    async def do():
        visit = await start_visit(db, client, body.concern_text, user.id)
        await audit(db, user, "start_visit", visit.id, body.model_dump(), {"visit_id": str(visit.id)}, "Staff started visit")
        await db.commit()
        return VisitOut.model_validate(visit)

    return await run_idempotent(db, request, user.id, do)


@router.get("/clients/{client_id}/notes", response_model=list[NoteOut])
async def list_notes(client_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Note).where(Note.client_id == client_id).order_by(Note.created_at.desc()))).scalars().all()


@router.post("/clients/{client_id}/notes", response_model=NoteOut, status_code=201)
async def create_note(client_id: uuid.UUID, body: NoteIn, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    client = await get_or_404(db, Client, client_id)
    visit = await db.get(Visit, body.visit_id) if body.visit_id else None
    note = await add_note(db, client, visit, body.text, body.tags, author_type="staff", author_user_id=user.id)
    await db.commit()
    return note
