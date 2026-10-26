"""A client on their own phone: sign in with mobile + OTP, see their consultations, chat with the clinic.

Everything here is scoped to the signed-in client; another client's visit or chat answers 404.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import Client, Visit
from app.schemas import ClientAuthStartIn, ClientAuthVerifyIn, ThreadMessageIn, ThreadOpenIn
from app.security import create_client_token, current_client
from app.services import DomainError
from app.services import threads as th
from app.services.clients import get_by_mobile, mask_mobile, normalise_mobile
from app.services.history import client_history
from app.services.otp import send_otp, verify_otp

router = APIRouter(tags=["client portal"])

WRONG_CODE = "That code is wrong or has expired. Please try again or ask for a new one."


@router.post("/client-auth/start")
async def start(body: ClientAuthStartIn, db: AsyncSession = Depends(get_db)):
    """Send a code if the number belongs to a client. The answer is the same either way, so it can't be used to find
    out who is a client of the clinic."""
    mobile = normalise_mobile(body.mobile)
    client = await get_by_mobile(db, mobile)
    if client is not None:
        await send_otp(db, mobile)
        await db.commit()
    return {"sent": True, "mock": get_settings().otp_mode == "mock",
            "message": "If this number is registered with the clinic, we've sent it a 6-digit code."}


@router.post("/client-auth/verify")
async def verify(body: ClientAuthVerifyIn, db: AsyncSession = Depends(get_db)):
    mobile = normalise_mobile(body.mobile)
    client = await get_by_mobile(db, mobile)
    if client is None:
        raise HTTPException(400, WRONG_CODE)
    try:
        ok = await verify_otp(db, mobile, body.code)
    except DomainError as e:
        await db.commit()  # keep the attempt count
        raise HTTPException(e.status if e.status != 410 else 400, e.message)
    await db.commit()
    if not ok:
        raise HTTPException(400, WRONG_CODE)
    return {"access_token": create_client_token(client), "client": _me(client)}


def _me(c: Client) -> dict:
    return {"id": str(c.id), "full_name": c.full_name, "first_name": c.full_name.split()[0], "mobile": mask_mobile(c.mobile)}


@router.get("/me")
async def me(client: Client = Depends(current_client), db: AsyncSession = Depends(get_db)):
    return {**_me(client), "unread": await th.unread_for_client(db, client.id)}


@router.get("/me/history")
async def history(client: Client = Depends(current_client), db: AsyncSession = Depends(get_db)):
    return await client_history(db, client.id)


@router.get("/me/visits/{visit_id}/summary")
async def summary(visit_id: uuid.UUID, client: Client = Depends(current_client), db: AsyncSession = Depends(get_db)):
    from app.services.summary import build_summary

    visit = await db.get(Visit, visit_id)
    if visit is None or visit.client_id != client.id:
        raise HTTPException(404, "Consultation not found")
    return await build_summary(db, visit)


@router.get("/me/threads")
async def threads(client: Client = Depends(current_client), db: AsyncSession = Depends(get_db)):
    return {"threads": await th.list_for_client(db, client.id)}


@router.post("/me/threads")
async def open_thread(body: ThreadOpenIn, client: Client = Depends(current_client), db: AsyncSession = Depends(get_db)):
    t = await th.get_or_create(db, client.id, body.visit_id)
    await db.commit()
    return await th.thread_out(db, t)


@router.get("/me/threads/{thread_id}")
async def read_thread(thread_id: uuid.UUID, after_seq: int = 0, client: Client = Depends(current_client),
                      db: AsyncSession = Depends(get_db)):
    t = await th.for_client(db, thread_id, client.id)
    msgs = await th.messages_after(db, t, after_seq)
    await th.mark_client_read(db, t)
    return {"thread": await th.thread_out(db, t), "messages": [th.message_out(m) for m in msgs]}


@router.post("/me/threads/{thread_id}/messages")
async def post(thread_id: uuid.UUID, body: ThreadMessageIn, client: Client = Depends(current_client),
               db: AsyncSession = Depends(get_db)):
    return await th.client_post(db, thread_id, client, body.text, body.client_message_id, channel="phone")
