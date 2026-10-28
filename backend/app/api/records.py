"""Consultation records for doctors and admins: every conversation and visit, whatever its outcome.

The review queue only shows what is waiting. Once a case is approved, rejected, handed over or closed it leaves the
queue, but the record (transcript, report, plan, decisions, notes and action log) stays and is found here.
"""
import re
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Approval, Client, Conversation, Escalation, Message, OtBooking, Prescription, User, Visit
from app.security import doctor_or_admin
from app.services.clients import age_of, mask_mobile
from app.services.escalation import TRIGGER_TEXT
from app.services.visits import programme_label

router = APIRouter(prefix="/doctor", tags=["records"])

OUTCOMES = {
    "completed": "Completed",
    "approved": "Approved, wrapping up",
    "awaiting_approval": "Awaiting approval",
    "escalated": "With a doctor",
    "in_progress": "In progress",
}


def outcome_of(conv: Conversation, visit: Visit | None) -> tuple[str, str]:
    """(code, label) for the list; the visit status is the source of truth once there is a visit."""
    if visit is not None:
        if conv.mode == "handed_over" and visit.status not in ("completed", "approved"):
            return "escalated", OUTCOMES["escalated"]
        return visit.status, OUTCOMES.get(visit.status, visit.status.replace("_", " ").capitalize())
    if conv.mode == "handed_over":
        return "escalated", "With a doctor (before the visit started)"
    if conv.mode == "closed":
        return "closed", "Closed by a doctor"
    return "not_started", "Stopped before the visit started"


@router.get("/records")
async def records(
    q: str | None = Query(default=None, max_length=100, description="Client name or part of the mobile number"),
    outcome: str | None = Query(default=None, description="completed | approved | awaiting_approval | escalated | "
                                                          "in_progress | rejected | not_started"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(doctor_or_admin),
    db: AsyncSession = Depends(get_db),
):
    has_esc = exists(select(Escalation.id).where(Escalation.conversation_id == Conversation.id))
    stmt = (select(Conversation, Client, Visit)
            .outerjoin(Client, Client.id == Conversation.client_id)
            .outerjoin(Visit, Visit.id == Conversation.visit_id)
            # An identified client, or something a doctor had to look at; skip abandoned anonymous starts.
            .where(or_(Conversation.client_id.isnot(None), has_esc)))
    if q and q.strip():
        term = q.strip()
        conds = [Client.full_name.ilike(f"%{term}%")]
        digits = re.sub(r"\D", "", term)
        if len(digits) >= 4:
            conds.append(Client.mobile.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    if outcome == "rejected":
        stmt = stmt.where(exists(select(Approval.id).where(Approval.visit_id == Visit.id, Approval.status == "rejected")))
    elif outcome == "escalated":
        stmt = stmt.where(or_(Visit.status == "escalated", Conversation.mode.in_(["handed_over", "closed"]), has_esc))
    elif outcome == "not_started":
        stmt = stmt.where(Conversation.visit_id.is_(None))
    elif outcome in OUTCOMES:
        stmt = stmt.where(Visit.status == outcome)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(Conversation.created_at.desc()).limit(limit).offset(offset))).all()

    names: dict[uuid.UUID, str] = {}

    async def name(uid: uuid.UUID | None) -> str | None:
        if uid is None:
            return None
        if uid not in names:
            u = await db.get(User, uid)
            names[uid] = u.full_name if u else "Staff"
        return names[uid]

    items = []
    for conv, client, visit in rows:
        approvals = (await db.execute(select(Approval).where(Approval.visit_id == visit.id).order_by(Approval.created_at))
                     ).scalars().all() if visit else []
        esc_conds = [Escalation.conversation_id == conv.id]
        if visit is not None:  # never compare with NULL: that would pull in other clients' pre-visit hand-overs
            esc_conds.append(Escalation.visit_id == visit.id)
        escs = (await db.execute(select(Escalation).where(or_(*esc_conds)).order_by(Escalation.created_at))).scalars().all()
        rx = (await db.execute(select(Prescription).where(Prescription.visit_id == visit.id))).scalar_one_or_none() if visit else None
        last_msg = await db.scalar(select(func.max(Message.created_at)).where(Message.conversation_id == conv.id))
        n_msgs = await db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conv.id))
        booked = await db.scalar(select(func.count(OtBooking.id)).where(OtBooking.visit_id == visit.id,
                                                                          OtBooking.status.in_(["tentative", "confirmed"]))) if visit else 0
        decided = [a for a in approvals if a.status != "pending"]
        doctors = []
        for uid in [a.doctor_id for a in decided] + [e.assigned_doctor_id for e in escs]:
            n = await name(uid)
            if n:
                doctors.append(n)
        code, label = outcome_of(conv, visit)
        items.append({
            "conversation_id": str(conv.id), "visit_id": str(visit.id) if visit else None,
            "client_name": client.full_name if client else "Unidentified client",
            "mobile": mask_mobile(client.mobile) if client else None, "age": age_of(client) if client else None,
            "started_at": conv.created_at.isoformat(), "last_activity": (last_msg or conv.created_at).isoformat(),
            "messages": n_msgs or 0,
            "programme": programme_label(visit.programme, visit.session_number) if visit else None,
            "session_number": visit.session_number if visit else None,
            "concern": visit.concern_text if visit else None,
            "outcome": code, "outcome_label": label,
            "plan_status": rx.status if rx else None, "plan_version": rx.version if rx else None,
            "decisions": [{"status": a.status, "doctor": names.get(a.doctor_id) if a.doctor_id else None,
                           "version": a.prescription_version,
                           "decided_at": a.decided_at.isoformat() if a.decided_at else None} for a in decided],
            "escalations": [{"trigger": e.trigger, "label": TRIGGER_TEXT.get(e.trigger, e.trigger).rstrip("."), "status": e.status,
                             "doctor": names.get(e.assigned_doctor_id) if e.assigned_doctor_id else None} for e in escs],
            "handled_by": sorted(set(doctors)) or (["AI assistant"] if visit else []),
            "treatments_booked": booked or 0,
        })
    return {"total": total, "limit": limit, "offset": offset, "items": items}
