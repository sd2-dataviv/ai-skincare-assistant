"""Prescription drafting, approval requests and doctor decisions (state machine in docs/02 §5)."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Approval, Assessment, Client, Escalation, Prescription, User, Visit
from app.services import DomainError
from app.services.recommendations import client_conditions, load_catalogue, safety_check


async def get(db: AsyncSession, visit: Visit) -> Prescription | None:
    return (await db.execute(select(Prescription).where(Prescription.visit_id == visit.id))).scalar_one_or_none()


async def _validate_items(db: AsyncSession, items: list[dict]) -> tuple[list[dict], dict]:
    catalogue = await load_catalogue(db)
    if not items:
        raise DomainError("empty_prescription", "A prescription needs at least one item.", 422)
    clean, seen = [], set()
    for raw in items:
        cid = raw.get("catalogue_id")
        if cid not in catalogue:
            # §04: the AI never makes up a service or product.
            raise DomainError("not_in_catalogue", f"'{cid}' is not in the clinic catalogue.", 422)
        if cid in seen:
            continue
        seen.add(cid)
        cat = catalogue[cid]
        sessions = raw.get("sessions")
        if cat.kind != "product":
            try:
                sessions = int(sessions or cat.default_sessions)
            except (TypeError, ValueError):
                raise DomainError("invalid_sessions", f"Sessions for {cat.name} must be a whole number.", 422)
            if not 1 <= sessions <= 24:
                raise DomainError("invalid_sessions", f"Sessions for {cat.name} must be between 1 and 24.", 422)
        else:
            sessions = None
        clean.append({
            "catalogue_id": cid, "kind": cat.kind, "name": cat.name, "sessions": sessions,
            "instructions": (raw.get("instructions") or cat.usage or None),
            "reason": (raw.get("reason") or "")[:500] or None, "price_inr": cat.price_inr,
        })
    return clean, catalogue


async def draft(db: AsyncSession, visit: Visit, client: Client, assessment: Assessment | None,
                items: list[dict], rationale: str | None, by_doctor: bool = False) -> Prescription:
    if visit.status == "escalated" and not by_doctor:
        raise DomainError("escalated", "A doctor has this visit; only the doctor can write the plan.")
    rx = await get(db, visit)
    if rx and rx.status in ("pending_approval", "approved"):
        raise DomainError("prescription_locked", f"The prescription is already {rx.status.replace('_', ' ')}.")
    clean, catalogue = await _validate_items(db, items)
    conditions = client_conditions(client, assessment)
    allergy_text = (assessment.flags or {}).get("allergy_text") if assessment else None
    kept, excluded, warnings = safety_check(clean, catalogue, conditions, allergy_text)
    if not kept:
        raise DomainError("all_items_blocked", "Every proposed item failed the safety checks.", 422)
    if rx is None:
        rx = Prescription(visit_id=visit.id, version=1)
        db.add(rx)
    elif rx.status == "rejected":
        rx.version += 1
    rx.items = kept
    rx.safety = {"excluded": excluded, "warnings": warnings, "conditions": sorted(conditions)}
    rx.rationale = (rationale or "")[:2000] or None
    rx.status = "draft"
    await db.flush()
    return rx


async def request_approval(db: AsyncSession, visit: Visit, by_doctor: bool = False) -> Approval:
    if visit.status == "escalated" and not by_doctor:
        raise DomainError("escalated", "A doctor has this visit; it can't be sent for approval automatically.")
    rx = await get(db, visit)
    if rx is None or rx.status != "draft":
        raise DomainError("no_draft", "There is no draft prescription to send for approval.")
    existing = (
        await db.execute(select(Approval).where(Approval.visit_id == visit.id, Approval.prescription_version == rx.version))
    ).scalar_one_or_none()
    if existing and existing.status == "pending":
        return existing
    rx.status = "pending_approval"
    visit.status = "awaiting_approval"
    ap = Approval(visit_id=visit.id, prescription_version=rx.version, status="pending")
    db.add(ap)
    await db.flush()
    return ap


async def pending_approval(db: AsyncSession, visit: Visit) -> Approval | None:
    return (
        await db.execute(select(Approval).where(Approval.visit_id == visit.id, Approval.status == "pending")
                         .order_by(Approval.created_at.desc()).limit(1))
    ).scalar_one_or_none()


async def decide(db: AsyncSession, visit: Visit, doctor: User, decision: str, items: list[dict] | None,
                 notes: str | None) -> Approval:
    """Doctor decision. approve | edit (approve with changes) | reject. Exactly once per pending approval."""
    if decision not in ("approve", "edit", "reject"):
        raise DomainError("invalid_decision", "Decision must be approve, edit or reject.", 422)
    # Row locks make a concurrent second decision wait, then fail the state check (exactly once).
    ap = (await db.execute(select(Approval).where(Approval.visit_id == visit.id, Approval.status == "pending")
                           .order_by(Approval.created_at.desc()).limit(1).with_for_update()
                           .execution_options(populate_existing=True))).scalar_one_or_none()
    rx = (await db.execute(select(Prescription).where(Prescription.visit_id == visit.id).with_for_update()
                           .execution_options(populate_existing=True))).scalar_one_or_none()
    if ap is None or rx is None or rx.status != "pending_approval" or ap.status != "pending":
        raise DomainError("not_pending", "This visit has no prescription awaiting approval.")
    now = datetime.now(timezone.utc)
    if decision == "edit":
        if not items:
            raise DomainError("edit_requires_items", "Provide the edited items.", 422)
        clean, catalogue = await _validate_items(db, items)
        # The doctor is the responsible prescriber: keep their items, but record any safety warnings.
        client = await db.get(Client, visit.client_id)
        assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
        _, excluded, warnings = safety_check(clean, catalogue, client_conditions(client, assessment),
                                             (assessment.flags or {}).get("allergy_text") if assessment else None)
        rx.items = clean
        rx.safety = {**(rx.safety or {}), "doctor_overrides": excluded, "warnings": warnings}
    if decision in ("approve", "edit"):
        rx.status = "approved"
        ap.status = "approved" if decision == "approve" else "edited"
        visit.status = "approved"
    else:
        rx.status = "rejected"
        ap.status = "rejected"
        visit.status = "escalated"
        db.add(Escalation(visit_id=visit.id, trigger="doctor_rejected", details=notes, assigned_doctor_id=doctor.id,
                          status="in_progress"))
    ap.doctor_id = doctor.id
    ap.notes = (notes or "")[:2000] or None
    ap.decided_at = now
    await db.flush()
    return ap
