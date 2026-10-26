"""AI session endpoints used by the clinic device."""
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import orchestrator
from app.api.common import get_or_404
from app.config import get_settings
from app.db import get_db
from app.models import Conversation, Message, User
from app.schemas import ConversationCreateIn, MessageIn, ThreadMessageIn, ThreadOpenIn
from app.security import staff
from app.services import model_images
from app.services.analysis import CONCERN_LABELS, latest
from app.services.state import load_state

router = APIRouter(tags=["conversations"])


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreateIn, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    # NFR-01: consent to the AI and to recording before the session starts.
    if not (body.consent_ai and body.consent_recording):
        raise HTTPException(400, "Consent to talk with an AI and to recording is required before starting.")
    return await orchestrator.start_conversation(db, user, body.language)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: uuid.UUID, after_seq: int = 0, _: User = Depends(staff),
                           db: AsyncSession = Depends(get_db)):
    await get_or_404(db, Conversation, conversation_id)
    msgs = (await db.execute(select(Message).where(Message.conversation_id == conversation_id, Message.seq > after_seq)
                             .order_by(Message.seq))).scalars().all()
    return await orchestrator.turn_payload(db, conversation_id, list(msgs))


@router.post("/conversations/{conversation_id}/messages")
async def post_message(conversation_id: uuid.UUID, body: MessageIn, user: User = Depends(staff),
                       db: AsyncSession = Depends(get_db)):
    await get_or_404(db, Conversation, conversation_id)
    return await orchestrator.handle_user_message(db, conversation_id, body.text, body.client_message_id, user)


@router.post("/conversations/{conversation_id}/photos")
async def post_photo(conversation_id: uuid.UUID, pose: str = Form(...), file: UploadFile = File(...),
                     user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    await get_or_404(db, Conversation, conversation_id)
    data = await file.read()
    return await orchestrator.handle_photo(db, conversation_id, pose, data, file.content_type or "", user)


@router.get("/conversations/{conversation_id}/history")
async def get_history(conversation_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    """The verified client's own past consultations and upcoming treatments (nothing before OTP verification)."""
    from app.services.history import client_history

    conv = await get_or_404(db, Conversation, conversation_id)
    if conv.client_id is None:
        return {"visits": [], "upcoming": []}
    return await client_history(db, conv.client_id, exclude=conv.visit_id)


@router.get("/conversations/{conversation_id}/visits/{visit_id}/summary")
async def get_past_summary(conversation_id: uuid.UUID, visit_id: uuid.UUID, _: User = Depends(staff),
                           db: AsyncSession = Depends(get_db)):
    """A past visit's approved summary, only for the client verified in this conversation."""
    from app.models import Visit
    from app.services.summary import build_summary

    conv = await get_or_404(db, Conversation, conversation_id)
    visit = await db.get(Visit, visit_id)
    if visit is None or conv.client_id is None or visit.client_id != conv.client_id:
        raise HTTPException(404, "Visit not found")
    return await build_summary(db, visit)


@router.get("/conversations/{conversation_id}/results")
async def get_results(conversation_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    """Results screen: photos, AI findings and the draft plan (draft shown only if policy allows)."""
    s = await load_state(db, conversation_id)
    if s.visit is None:
        raise HTTPException(404, "No visit yet")
    diag = latest(s.analyses, "diagnosis")
    aa = latest(s.analyses, "anti_aging")
    cmp = latest(s.analyses, "compare")
    rx = s.prescription
    show_plan = rx is not None and (rx.status == "approved" or get_settings().client_sees_draft_plan)
    return {
        "visit_id": str(s.visit.id),
        "stage": s.stage,
        "photos": [{"id": str(p.id), "pose": p.pose, "accepted": p.accepted} for p in s.photos if p.accepted],
        "findings": [{**v, "concern": c, "label": CONCERN_LABELS.get(c, c), "severity": v["label"]}
                     for c, v in sorted((diag.result["diagnosis"] if diag else {}).items(), key=lambda kv: -kv[1]["score"])],
        "confidence": diag.confidence if diag else None,
        "engine": diag.engine if diag else None,
        "anti_aging": aa.result if aa else None,
        "comparison": cmp.result if cmp else None,
        "skin_type": s.assessment.skin_type if s.assessment else None,
        "sensitive": bool(s.assessment and s.assessment.sensitive),
        "concerns": list(s.visit.concerns or []),
        "images": await model_images.client_list(db, s.visit.id),  # empty until the plan is approved
        "plan": ({"status": rx.status, "version": rx.version, "items": rx.items,
                  "warnings": (rx.safety or {}).get("warnings", []), "excluded": (rx.safety or {}).get("excluded", [])}
                 if show_plan else ({"status": rx.status, "hidden": True} if rx else None)),
    }


# ── The verified client's chats with the clinic, on the clinic device ────────
async def _verified_client(db: AsyncSession, conversation_id: uuid.UUID):
    from app.models import Client

    conv = await get_or_404(db, Conversation, conversation_id)
    if conv.client_id is None:
        raise HTTPException(403, "Verify the client's mobile number first.")
    return await db.get(Client, conv.client_id)


@router.get("/conversations/{conversation_id}/threads")
async def list_threads(conversation_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    from app.services import threads as th

    conv = await get_or_404(db, Conversation, conversation_id)
    if conv.client_id is None:
        return {"threads": []}
    return {"threads": await th.list_for_client(db, conv.client_id)}


@router.post("/conversations/{conversation_id}/threads")
async def open_thread(conversation_id: uuid.UUID, body: ThreadOpenIn, _: User = Depends(staff),
                      db: AsyncSession = Depends(get_db)):
    from app.services import threads as th

    client = await _verified_client(db, conversation_id)
    t = await th.get_or_create(db, client.id, body.visit_id)
    await db.commit()
    return await th.thread_out(db, t)


@router.get("/conversations/{conversation_id}/threads/{thread_id}")
async def read_thread(conversation_id: uuid.UUID, thread_id: uuid.UUID, after_seq: int = 0, _: User = Depends(staff),
                      db: AsyncSession = Depends(get_db)):
    from app.services import threads as th

    client = await _verified_client(db, conversation_id)
    t = await th.for_client(db, thread_id, client.id)
    msgs = await th.messages_after(db, t, after_seq)
    await th.mark_client_read(db, t)
    return {"thread": await th.thread_out(db, t), "messages": [th.message_out(m) for m in msgs]}


@router.post("/conversations/{conversation_id}/threads/{thread_id}/messages")
async def post_thread_message(conversation_id: uuid.UUID, thread_id: uuid.UUID, body: ThreadMessageIn,
                              _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    from app.services import threads as th

    client = await _verified_client(db, conversation_id)
    return await th.client_post(db, thread_id, client, body.text, body.client_message_id, channel="clinic device")
