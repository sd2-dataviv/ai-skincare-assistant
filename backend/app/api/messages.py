"""Doctors and admins chatting with clients: the list of chats, opening one with any client, replying, handing back."""
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import get_or_404
from app.db import get_db
from app.models import Client, Thread, User
from app.schemas import StaffThreadOpenIn, ThreadMessageIn
from app.security import doctor_or_admin
from app.services import threads as th

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/threads")
async def list_threads(waiting: bool = False, client_id: uuid.UUID | None = None,
                       q: str | None = Query(default=None, max_length=100),
                       _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    items = await th.list_for_staff(db, waiting_only=waiting, client_id=client_id, q=q)
    return {"threads": items, "waiting": sum(1 for i in items if i["waiting"])}


@router.post("/threads")
async def open_thread(body: StaffThreadOpenIn, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Open (or find) the chat with any client, about one of their visits or in general."""
    await get_or_404(db, Client, body.client_id)
    t = await th.get_or_create(db, body.client_id, body.visit_id, created_by=user)
    await db.commit()
    return await th.thread_out(db, t, for_staff=True)


@router.get("/threads/{thread_id}")
async def read_thread(thread_id: uuid.UUID, after_seq: int = 0, _: User = Depends(doctor_or_admin),
                      db: AsyncSession = Depends(get_db)):
    t = await get_or_404(db, Thread, thread_id)
    msgs = await th.messages_after(db, t, after_seq)
    await th.mark_staff_read(db, t)
    return {"thread": await th.thread_out(db, t, for_staff=True), "messages": [th.message_out(m) for m in msgs]}


@router.post("/threads/{thread_id}/messages")
async def reply(thread_id: uuid.UUID, body: ThreadMessageIn, user: User = Depends(doctor_or_admin),
                db: AsyncSession = Depends(get_db)):
    return await th.staff_post(db, thread_id, user, body.text)


@router.post("/threads/{thread_id}/hand-back")
async def hand_back(thread_id: uuid.UUID, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    return await th.hand_back(db, thread_id, user)
