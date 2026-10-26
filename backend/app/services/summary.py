"""Visit summary, sharing, notes and next-session planning (spec §02 wrap-up)."""
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations.messaging import get_messenger
from app.models import Analysis, Approval, Assessment, CatalogueItem, Client, Note, SummaryShare, User, Visit
from app.services import DomainError
from app.services.analysis import CONCERN_LABELS, latest, top_findings
from app.services.clients import mask_mobile
from app.services.prescriptions import get as get_prescription
from app.services.visits import programme_label

DISCLAIMER = ("This plan was prepared with AI assistance and approved by a registered medical practitioner. "
              "Contact the clinic if you notice irritation or any unexpected reaction.")


async def add_note(db: AsyncSession, client: Client, visit: Visit | None, text: str, tags: list[str],
                   author_type: str = "ai", author_user_id=None) -> Note:
    text = (text or "").strip()
    if not text:
        raise DomainError("empty_note", "Note text is required.", 422)
    clean_tags = sorted({t.strip().lower().replace(" ", "_")[:40] for t in tags or [] if t and t.strip()})[:12]
    note = Note(client_id=client.id, visit_id=visit.id if visit else None, author_type=author_type,
                author_user_id=author_user_id, text=text[:4000], tags=clean_tags)
    db.add(note)
    await db.flush()
    return note


async def set_next_session(db: AsyncSession, visit: Visit) -> date | None:
    rx = await get_prescription(db, visit)
    if rx is None or rx.status != "approved":
        raise DomainError("not_approved", "The plan must be approved before scheduling the next session.")
    from app.services import ot

    booked = [b for b in await ot.visit_bookings(db, visit.id) if b.status in ot.HOLD and b.starts_at]
    if booked:
        # The first booked treatment is the next time the client is in the clinic.
        visit.next_session_date = min(b.starts_at for b in booked).astimezone(ot.clinic_tz()).date()
        await db.flush()
        return visit.next_session_date
    ids = [i["catalogue_id"] for i in rx.items if i["kind"] == "service"]
    intervals = []
    if ids:
        rows = (await db.execute(select(CatalogueItem).where(CatalogueItem.id.in_(ids)))).scalars().all()
        intervals = [r.interval_days for r in rows if r.interval_days]
    days = min(intervals) if intervals else 30  # products only: review in a month
    visit.next_session_date = (visit.created_at.date() if visit.created_at else date.today()) + timedelta(days=days)
    await db.flush()
    return visit.next_session_date


async def build_summary(db: AsyncSession, visit: Visit) -> dict:
    rx = await get_prescription(db, visit)
    if rx is None or rx.status != "approved":
        # NFR-03: nothing reaches the user without doctor approval.
        raise DomainError("not_approved", "The summary is available once a doctor has approved the plan.", 403)
    client = await db.get(Client, visit.client_id)
    assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
    analyses = (await db.execute(select(Analysis).where(Analysis.visit_id == visit.id).order_by(Analysis.created_at))).scalars().all()
    approval = (await db.execute(select(Approval).where(Approval.visit_id == visit.id, Approval.status.in_(["approved", "edited"]))
                                 .order_by(Approval.decided_at.desc()).limit(1))).scalar_one_or_none()
    doctor = await db.get(User, approval.doctor_id) if approval and approval.doctor_id else None
    diag = latest(list(analyses), "diagnosis")
    aa = latest(list(analyses), "anti_aging")
    cmp = latest(list(analyses), "compare")
    findings = []
    if diag:
        for f in top_findings(diag.result["diagnosis"], list(visit.concerns or [])):
            findings.append({"concern": f["concern"], "label": CONCERN_LABELS.get(f["concern"], f["concern"]),
                             "score": f["score"], "severity": f["label"]})
    return {
        "visit_id": str(visit.id),
        "client_first_name": client.full_name.split()[0] if client else "",
        "date": visit.created_at.date().isoformat(),
        "programme": programme_label(visit.programme),
        "session_number": visit.session_number,
        "skin_type": assessment.skin_type if assessment else None,
        "sensitive": bool(assessment and assessment.sensitive),
        "findings": findings,
        "skin_age": (aa.result or {}).get("skin_age") if aa else None,
        "improvement": (cmp.result if cmp else None),
        "plan": [{k: i.get(k) for k in ("kind", "name", "sessions", "instructions", "reason")} for i in rx.items],
        "warnings": (rx.safety or {}).get("warnings", []),
        "approved_by": {"name": doctor.full_name, "registration_no": doctor.registration_no} if doctor else None,
        "approval_status": approval.status if approval else None,
        "next_session_date": visit.next_session_date.isoformat() if visit.next_session_date else None,
        "appointments": [{k: a[k] for k in ("procedure", "status", "label", "starts_at", "ends_at", "room_name",
                                             "session_index", "sessions_total")}
                         for a in await _appointments(db, visit)],
        "disclaimer": DISCLAIMER,
    }


async def _appointments(db: AsyncSession, visit: Visit) -> list[dict]:
    from app.services import ot

    return await ot.visit_appointments(db, visit)


async def share(db: AsyncSession, visit: Visit, channel: str) -> dict:
    if channel not in ("sms", "whatsapp", "link"):
        raise DomainError("invalid_channel", "Channel must be sms, whatsapp or link.", 422)
    await build_summary(db, visit)  # enforces approval
    s = get_settings()
    now = datetime.now(timezone.utc)
    existing = (await db.execute(select(SummaryShare).where(
        SummaryShare.visit_id == visit.id, SummaryShare.channel == channel, SummaryShare.expires_at > now))).scalar_one_or_none()
    client = await db.get(Client, visit.client_id)
    if existing:  # exactly once per channel
        share_row = existing
    else:
        share_row = SummaryShare(visit_id=visit.id, channel=channel, token=secrets.token_urlsafe(24),
                                 destination_masked=mask_mobile(client.mobile) if channel != "link" else None,
                                 expires_at=now + timedelta(days=s.summary_link_ttl_days))
        db.add(share_row)
        await db.flush()
        url = f"{s.public_base_url.rstrip('/')}/s/{share_row.token}"
        if channel != "link":
            booked = [a for a in await _appointments(db, visit) if a["status"] in ("tentative", "confirmed")]
            when = (f" {booked[0]['procedure']}: {booked[0]['label']}"
                    + (" (to be confirmed)." if booked[0]["status"] == "tentative" else ".")) if booked else ""
            share_row.delivery_status = await get_messenger().send(
                channel, client.mobile, f"Hi {client.full_name.split()[0]}, your skin plan is ready: {url}{when}")
    return {"channel": channel, "token": share_row.token,
            "url": f"{s.public_base_url.rstrip('/')}/s/{share_row.token}",
            "destination": share_row.destination_masked, "expires_at": share_row.expires_at.isoformat()}


async def complete_visit(db: AsyncSession, visit: Visit) -> None:
    if visit.status == "approved":
        visit.status = "completed"
        visit.completed_at = datetime.now(timezone.utc)
        await db.flush()
