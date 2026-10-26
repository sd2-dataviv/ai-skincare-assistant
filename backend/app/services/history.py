"""A returning client's own history, shown to them on the clinic device once their mobile is verified.

Only what the client is allowed to see: findings and plans of visits a doctor approved (NFR-03), their treatment
appointments, and the date and status of other visits. Nothing about any other client.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Analysis, CatalogueItem, OtBooking, OtRoom, Prescription, Visit
from app.services import ot
from app.services.analysis import CONCERN_LABELS, latest, top_findings
from app.services.visits import programme_label

CLIENT_STATUS = {
    "completed": "Completed",
    "approved": "Plan approved",
    "awaiting_approval": "Not finished",
    "in_progress": "Not finished",
    "escalated": "Handled by a doctor",
}


async def count_past(db: AsyncSession, client_id: uuid.UUID, exclude: uuid.UUID | None) -> int:
    q = select(func.count(Visit.id)).where(Visit.client_id == client_id)
    if exclude is not None:
        q = q.where(Visit.id != exclude)
    return await db.scalar(q) or 0


async def client_history(db: AsyncSession, client_id: uuid.UUID, exclude: uuid.UUID | None = None) -> dict:
    q = select(Visit).where(Visit.client_id == client_id)
    if exclude is not None:
        q = q.where(Visit.id != exclude)
    visits = (await db.execute(q.order_by(Visit.created_at.desc()))).scalars().all()
    out = []
    for v in visits:
        rx = (await db.execute(select(Prescription).where(Prescription.visit_id == v.id))).scalar_one_or_none()
        approved = rx is not None and rx.status == "approved"
        item = {"visit_id": str(v.id), "date": v.created_at.date().isoformat(), "programme": programme_label(v.programme),
                "session_number": v.session_number, "concern": v.concern_text,
                "status": CLIENT_STATUS.get(v.status, v.status.replace("_", " ").capitalize()), "approved": approved,
                "findings": [], "plan": [], "next_session_date": None, "appointments": []}
        if approved:
            analyses = (await db.execute(select(Analysis).where(Analysis.visit_id == v.id)
                                         .order_by(Analysis.created_at))).scalars().all()
            diag = latest(list(analyses), "diagnosis")
            if diag:
                item["findings"] = [{"label": CONCERN_LABELS.get(f["concern"], f["concern"]), "severity": f["label"],
                                     "score": round(f["score"])} for f in top_findings(diag.result["diagnosis"],
                                                                                        list(v.concerns or []))]
            item["plan"] = [{"name": i.get("name"), "kind": i.get("kind"), "sessions": i.get("sessions"),
                             "instructions": i.get("instructions")} for i in rx.items]
            item["next_session_date"] = v.next_session_date.isoformat() if v.next_session_date else None
            item["appointments"] = [{k: a[k] for k in ("procedure", "status", "label", "starts_at", "room_name")}
                                    for a in await ot.visit_appointments(db, v)]
        out.append(item)

    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(OtBooking, CatalogueItem, OtRoom)
                             .join(CatalogueItem, CatalogueItem.id == OtBooking.catalogue_item_id)
                             .join(OtRoom, OtRoom.id == OtBooking.room_id)
                             .where(OtBooking.client_id == client_id, OtBooking.status.in_(ot.HOLD),
                                    OtBooking.starts_at > now)
                             .order_by(OtBooking.starts_at))).all()
    upcoming = [{"procedure": i.name, "label": ot.slot_label(b.starts_at), "starts_at": b.starts_at.isoformat(),
                 "room_name": r.name, "session_index": b.session_index, "sessions_total": b.sessions_total,
                 "tentative": b.status == "tentative"}
                for b, i, r in rows]
    return {"visits": out, "upcoming": upcoming}
