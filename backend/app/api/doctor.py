"""Doctor review: queue of drafts and escalations; approve, edit, take over, hand back (spec §05, §07)."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import orchestrator
from app.api.common import audit, get_or_404
from app.db import get_db
from app.idempotency import run_idempotent
from app.models import Approval, Assessment, Client, Conversation, Escalation, Prescription, User, Visit
from app.schemas import DoctorMessageIn, PrescriptionIn, ResolveIn
from app.security import doctor_only, doctor_or_admin
from app.services import prescriptions as rx_svc
from app.services.clients import age_of
from app.services.escalation import TRIGGER_TEXT
from app.services.visits import programme_label

router = APIRouter(prefix="/doctor", tags=["doctor"])


def _minutes_since(t: datetime) -> int:
    return int((datetime.now(timezone.utc) - t).total_seconds() // 60)


@router.get("/queue")
async def queue(_: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    items = []
    rows = (await db.execute(
        select(Approval, Visit, Client).join(Visit, Visit.id == Approval.visit_id).join(Client, Client.id == Visit.client_id)
        .where(Approval.status == "pending").order_by(Approval.created_at)
    )).all()
    for ap, visit, client in rows:
        items.append({"type": "approval", "id": str(ap.id), "visit_id": str(visit.id), "client_name": client.full_name,
                      "age": age_of(client), "programme": programme_label(visit.programme, visit.session_number),
                      "session_number": visit.session_number, "summary": "Draft plan awaiting approval",
                      "created_at": ap.created_at.isoformat(), "waiting_minutes": _minutes_since(ap.created_at),
                      "status": ap.status})
    escs = (await db.execute(select(Escalation).where(Escalation.status != "resolved").order_by(Escalation.created_at))).scalars().all()
    for esc in escs:
        visit = await db.get(Visit, esc.visit_id) if esc.visit_id else None
        conv = await db.get(Conversation, esc.conversation_id) if esc.conversation_id else None
        client = None
        if visit:
            client = await db.get(Client, visit.client_id)
        elif conv and conv.client_id:
            client = await db.get(Client, conv.client_id)
        doctor = await db.get(User, esc.assigned_doctor_id) if esc.assigned_doctor_id else None
        items.append({"type": "escalation", "id": str(esc.id), "visit_id": str(visit.id) if visit else None,
                      "conversation_id": str(esc.conversation_id) if esc.conversation_id else None,
                      "client_name": client.full_name if client else "Unidentified client",
                      "age": age_of(client) if client else None,
                      "programme": programme_label(visit.programme, visit.session_number) if visit else None,
                      "session_number": visit.session_number if visit else None,
                      "trigger": esc.trigger, "summary": TRIGGER_TEXT.get(esc.trigger, esc.trigger),
                      "details": esc.details, "created_at": esc.created_at.isoformat(),
                      "waiting_minutes": _minutes_since(esc.created_at), "status": esc.status,
                      "assigned_to": doctor.full_name if doctor else None})
    listed = {i["visit_id"] for i in items if i["visit_id"]}
    held = (await db.execute(select(Visit, Client).join(Client, Client.id == Visit.client_id)
                             .where(Visit.status == "escalated").order_by(Visit.created_at))).all()
    for visit, client in held:
        if str(visit.id) in listed:
            continue
        items.append({"type": "with_doctor", "id": str(visit.id), "visit_id": str(visit.id), "client_name": client.full_name,
                      "age": age_of(client), "programme": programme_label(visit.programme, visit.session_number),
                      "session_number": visit.session_number, "summary": "With a doctor: plan still to be written",
                      "created_at": visit.created_at.isoformat(), "waiting_minutes": _minutes_since(visit.created_at),
                      "status": visit.status})
    # Clients waiting for a reply in a follow-up chat
    from app.models import Thread
    from app.services.threads import HANDOVER_TEXT

    waiting = (await db.execute(select(Thread, Client).join(Client, Client.id == Thread.client_id)
                                .where(Thread.waiting_since.is_not(None)).order_by(Thread.waiting_since))).all()
    for t, client in waiting:
        visit = await db.get(Visit, t.visit_id) if t.visit_id else None
        items.append({"type": "message", "id": str(t.id), "thread_id": str(t.id),
                      "visit_id": str(visit.id) if visit else None, "client_name": client.full_name,
                      "age": age_of(client), "programme": programme_label(visit.programme, visit.session_number) if visit else None,
                      "session_number": visit.session_number if visit else None, "trigger": t.handover_reason,
                      "urgent": t.handover_reason == "medical_concern",
                      "summary": "Waiting for a reply: " + HANDOVER_TEXT.get(t.handover_reason or "",
                                                                             "the client wrote to the clinic team.").lower(),
                      "details": t.handover_details, "created_at": t.waiting_since.isoformat(),
                      "waiting_minutes": _minutes_since(t.waiting_since), "status": "waiting"})
    # Escalations first (safety), urgent chats with them, then oldest first.
    items.sort(key=lambda i: (0 if i["type"] == "escalation" or i.get("urgent") else
                              {"with_doctor": 1, "approval": 2}.get(i["type"], 3), i["created_at"]))
    return {"items": items, "counts": {"approvals": sum(i["type"] == "approval" for i in items),
                                       "escalations": sum(i["type"] == "escalation" for i in items),
                                       "messages": sum(i["type"] == "message" for i in items)}}


@router.get("/conversations/{conversation_id}")
async def conversation_transcript(conversation_id: uuid.UUID, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    from app.models import Message

    conv = await get_or_404(db, Conversation, conversation_id)
    msgs = (await db.execute(select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq))).scalars().all()
    client = await db.get(Client, conv.client_id) if conv.client_id else None
    escs = (await db.execute(select(Escalation).where(Escalation.conversation_id == conv.id).order_by(Escalation.created_at))
            ).scalars().all()
    history = []
    for e in escs:
        doc = await db.get(User, e.assigned_doctor_id) if e.assigned_doctor_id else None
        history.append({"id": str(e.id), "trigger": e.trigger, "label": TRIGGER_TEXT.get(e.trigger, e.trigger).rstrip("."),
                        "details": e.details, "status": e.status, "doctor": doc.full_name if doc else None,
                        "created_at": e.created_at.isoformat(),
                        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None})
    return {"conversation_id": str(conv.id), "mode": conv.mode, "visit_id": str(conv.visit_id) if conv.visit_id else None,
            "client_name": client.full_name if client else None, "consent_at": conv.consent_at.isoformat(),
            "client": {"full_name": client.full_name, "age": age_of(client), "gender": client.gender} if client else None,
            "started_at": conv.created_at.isoformat(), "escalations": history,
            "messages": [orchestrator.message_out(m) for m in msgs]}


@router.post("/escalations/{escalation_id}/take-over")
async def take_over(escalation_id: uuid.UUID, doctor: User = Depends(doctor_only), db: AsyncSession = Depends(get_db)):
    esc = await get_or_404(db, Escalation, escalation_id)
    if esc.status == "resolved":
        raise HTTPException(409, "Escalation already resolved")
    esc.status = "in_progress"
    esc.assigned_doctor_id = doctor.id
    conv = await db.get(Conversation, esc.conversation_id) if esc.conversation_id else (
        await orchestrator.latest_conversation_for_visit(db, esc.visit_id) if esc.visit_id else None)
    await audit(db, doctor, "take_over", esc.visit_id, {"escalation_id": str(esc.id)}, {}, "Doctor took over")
    msg = await orchestrator.doctor_joined(db, conv, doctor) if conv else None
    await db.commit()
    return {"status": esc.status, "message": orchestrator.message_out(msg) if msg else None}


@router.post("/escalations/{escalation_id}/resolve")
async def resolve(escalation_id: uuid.UUID, body: ResolveIn, doctor: User = Depends(doctor_only), db: AsyncSession = Depends(get_db)):
    esc = await get_or_404(db, Escalation, escalation_id)
    if esc.status == "resolved":
        raise HTTPException(409, "Escalation already resolved")
    if body.hand_back_to_ai and esc.trigger == "doctor_rejected":
        raise HTTPException(409, "You rejected the AI's plan, so write the plan with Prescribe instead of handing back.")
    await audit(db, doctor, "resolve_escalation", esc.visit_id, body.model_dump(), {}, "Doctor resolved escalation")
    if body.hand_back_to_ai:
        msgs = await orchestrator.hand_back(db, esc, doctor)
    else:
        from app.services.escalation import resolve as resolve_svc
        conv = await db.get(Conversation, esc.conversation_id) if esc.conversation_id else None
        visit = await db.get(Visit, esc.visit_id) if esc.visit_id else None
        await resolve_svc(db, esc, conv, visit, hand_back_to_ai=False)
        await db.commit()
        msgs = []
    return {"status": "resolved", "conversation_messages": msgs}


@router.post("/conversations/{conversation_id}/messages")
async def doctor_message(conversation_id: uuid.UUID, body: DoctorMessageIn, doctor: User = Depends(doctor_or_admin),
                         db: AsyncSession = Depends(get_db)):
    conv = await get_or_404(db, Conversation, conversation_id)
    if conv.mode == "ai":
        conv.mode = "handed_over"
    m = await orchestrator.add_message(db, conv.id, "doctor", body.text, {"doctor": doctor.full_name, "doctor_id": str(doctor.id),
                                                                       "author_role": doctor.role})
    await db.commit()
    return orchestrator.message_out(m)


@router.post("/visits/{visit_id}/prescribe")
async def prescribe(visit_id: uuid.UUID, body: PrescriptionIn, request: Request, doctor: User = Depends(doctor_only),
                    db: AsyncSession = Depends(get_db)):
    """The doctor writes (or rewrites) the plan directly and approves it in one step."""
    visit = await get_or_404(db, Visit, visit_id)
    client = await db.get(Client, visit.client_id)

    async def do():
        items = [i.model_dump() for i in body.items]
        rx = (await db.execute(select(Prescription).where(Prescription.visit_id == visit.id))).scalar_one_or_none()
        if rx is not None and rx.status == "approved":
            raise HTTPException(409, "Plan already approved")
        if rx is None or rx.status in ("draft", "rejected"):
            assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
            await rx_svc.draft(db, visit, client, assessment, items, body.rationale or "Written by the doctor.", by_doctor=True)
            await rx_svc.request_approval(db, visit, by_doctor=True)
            ap = await rx_svc.decide(db, visit, doctor, "approve", None, "Prescribed directly by the doctor.")
        else:
            ap = await rx_svc.decide(db, visit, doctor, "edit", items, "Edited and approved by the doctor.")
        await audit(db, doctor, "doctor_prescribe", visit.id, {"items": [i["catalogue_id"] for i in items]}, {"status": ap.status},
                    "Doctor prescribed")
        await db.commit()
        messages = await orchestrator.on_approval_decided(db, visit, "approve" if ap.status == "approved" else "edit", doctor)
        return {"status": ap.status, "conversation_messages": messages}

    return await run_idempotent(db, request, doctor.id, do)
