import uuid

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AIAction, Analysis, Approval, Assessment, Client, Conversation, Escalation, Note, Photo, Prescription, User, Visit
from app.schemas import AnalysisOut, ApprovalOut, AssessmentOut, ClientOut, EscalationOut, PhotoOut, PrescriptionOut, VisitOut
from app.services import model_images


async def get_or_404(db: AsyncSession, model, id_):
    obj = await db.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


async def audit(db: AsyncSession, user: User, action: str, visit_id: uuid.UUID | None, inputs: dict, result: dict,
                reason: str, status: str = "ok") -> None:
    """Staff actions through REST are logged in the same audit table as AI actions."""
    db.add(AIAction(visit_id=visit_id, action=action, actor="staff", inputs=jsonable_encoder(inputs),
                    result=jsonable_encoder(result), reason=f"{reason} (by {user.role} {user.full_name})", status=status))


async def _people(db: AsyncSession, ids: list) -> dict[str, str]:
    out = {}
    for i in {i for i in ids if i}:
        u = await db.get(User, i)
        if u:
            out[str(i)] = u.full_name
    return out


async def _ot(db: AsyncSession, visit: Visit) -> list[dict]:
    from app.services import ot

    return await ot.visit_appointments(db, visit)


async def visit_detail(db: AsyncSession, visit: Visit, include_actions: bool = True) -> dict:
    vid = visit.id
    client = await db.get(Client, visit.client_id)
    assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == vid))).scalar_one_or_none()
    photos = (await db.execute(select(Photo).where(Photo.visit_id == vid).order_by(Photo.created_at))).scalars().all()
    analyses = (await db.execute(select(Analysis).where(Analysis.visit_id == vid).order_by(Analysis.created_at))).scalars().all()
    rx = (await db.execute(select(Prescription).where(Prescription.visit_id == vid))).scalar_one_or_none()
    approvals = (await db.execute(select(Approval).where(Approval.visit_id == vid).order_by(Approval.created_at))).scalars().all()
    conv = (await db.execute(select(Conversation).where(Conversation.visit_id == vid).order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()
    esc_q = select(Escalation).where(Escalation.visit_id == vid)
    if conv is not None:
        from sqlalchemy import or_
        esc_q = select(Escalation).where(or_(Escalation.visit_id == vid, Escalation.conversation_id == conv.id))
    escalations = (await db.execute(esc_q.order_by(Escalation.created_at))).scalars().all()
    out = {
        "visit": VisitOut.model_validate(visit).model_dump(mode="json"),
        "client": ClientOut.model_validate(client).model_dump(mode="json"),
        "assessment": AssessmentOut.model_validate(assessment).model_dump(mode="json") if assessment else None,
        "photos": [PhotoOut.model_validate(p).model_dump(mode="json") for p in photos],
        "analyses": [AnalysisOut.model_validate(a).model_dump(mode="json") for a in analyses],
        "prescription": PrescriptionOut.model_validate(rx).model_dump(mode="json") if rx else None,
        "approvals": [ApprovalOut.model_validate(a).model_dump(mode="json") for a in approvals],
        "escalations": [EscalationOut.model_validate(e).model_dump(mode="json") for e in escalations],
        "conversation_id": str(conv.id) if conv else None,
        "ot_bookings": await _ot(db, visit),
        "people": await _people(db, [a.doctor_id for a in approvals] + [e.assigned_doctor_id for e in escalations]),
        "notes": [{"id": str(n.id), "author_type": n.author_type, "text": n.text, "tags": list(n.tags or []),
                   "created_at": n.created_at.isoformat()}
                  for n in (await db.execute(select(Note).where(Note.visit_id == vid).order_by(Note.created_at))).scalars()],
        "conversation_mode": conv.mode if conv else None,
        "model_images": [model_images.staff_view(r) for r in await model_images.for_visit(db, vid)],
    }
    if include_actions:
        conds = [AIAction.visit_id == vid]
        if conv is not None:
            from sqlalchemy import or_
            actions = (await db.execute(select(AIAction).where(or_(AIAction.visit_id == vid, AIAction.conversation_id == conv.id))
                                        .order_by(AIAction.created_at))).scalars().all()
        else:
            actions = (await db.execute(select(AIAction).where(*conds).order_by(AIAction.created_at))).scalars().all()
        out["actions"] = [{"id": str(a.id), "action": a.action, "actor": a.actor, "status": a.status, "stage": a.stage,
                           "reason": a.reason, "inputs": a.inputs, "result": a.result,
                           "created_at": a.created_at.isoformat()} for a in actions]
    return out
