"""Operation theatre (procedure room) availability, calendar and bookings (FR-19).

The calendar shows every client's bookings, so it is for doctors and admins (front desk) only. The clinic device
(operator account) faces the client: it books a client's own treatments through the chat, never through these routes.

Every action the AI takes in the booking step has a staff endpoint here too (FR-17), and staff actions are audited.
"""
import uuid
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import orchestrator
from app.api.common import audit, get_or_404
from app.db import get_db
from app.idempotency import run_idempotent
from app.integrations.messaging import get_messenger
from app.models import CatalogueItem, Client, ClinicHoliday, DoctorHours, OtBlock, OtBooking, OtEquipment, OtRoom, User, Visit
from app.security import admin_only, doctor_or_admin, staff
from app.services import ot as ot_svc
from app.services import ot_reports

router = APIRouter(prefix="/ot", tags=["ot"])


class RoomIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(min_length=1)
    open_time: time = time(10)
    close_time: time = time(19)
    open_days: list[int] = [0, 1, 2, 3, 4, 5]
    buffer_minutes: int = Field(default=15, ge=0, le=120)
    active: bool = True


class RoomPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    capabilities: list[str] | None = None
    open_time: time | None = None
    close_time: time | None = None
    open_days: list[int] | None = None
    buffer_minutes: int | None = Field(default=None, ge=0, le=120)
    active: bool | None = None


class BlockIn(BaseModel):
    room_id: uuid.UUID | None = None
    doctor_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=200)


class BookingIn(BaseModel):
    visit_id: uuid.UUID
    item_id: str
    room_id: uuid.UUID
    starts_at: datetime
    doctor_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    override_reason: str | None = Field(default=None, max_length=300)


class CheckIn(BaseModel):
    item_id: str
    room_id: uuid.UUID
    starts_at: datetime
    doctor_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None  # when moving an existing booking, ignore it


class RescheduleIn(BaseModel):
    version: int
    starts_at: datetime
    room_id: uuid.UUID | None = None
    doctor_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    override_reason: str | None = Field(default=None, max_length=300)


class VersionIn(BaseModel):
    version: int


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)
    version: int | None = None


class StageIn(BaseModel):
    stage: str
    version: int
    at: datetime | None = None


class ReadinessIn(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=300)


class EquipmentIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    equipment_type: str = Field(min_length=1, max_length=40)
    buffer_minutes: int = Field(default=0, ge=0, le=240)
    active: bool = True


class EquipmentPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    equipment_type: str | None = Field(default=None, max_length=40)
    buffer_minutes: int | None = Field(default=None, ge=0, le=240)
    active: bool | None = None


class HoursRow(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time


class HoursIn(BaseModel):
    hours: list[HoursRow]


class HolidayIn(BaseModel):
    day: date
    name: str = Field(min_length=1, max_length=100)


def _check_room(body: RoomIn | RoomPatch) -> None:
    if body.open_days is not None and any(d < 0 or d > 6 for d in body.open_days):
        raise HTTPException(422, "open_days are 0 (Monday) to 6 (Sunday).")
    if body.open_time and body.close_time and body.close_time <= body.open_time:
        raise HTTPException(422, "close_time must be after open_time.")


# ── Rooms ────────────────────────────────────────────────────────────────────
@router.get("/rooms")
async def list_rooms(_: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    rooms = (await db.execute(select(OtRoom).order_by(OtRoom.code))).scalars()
    return [ot_svc.room_out(r) for r in rooms]


@router.post("/rooms", status_code=201)
async def create_room(body: RoomIn, user: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    _check_room(body)
    if (await db.execute(select(OtRoom).where(OtRoom.code == body.code))).scalar_one_or_none():
        raise HTTPException(409, f"Room {body.code} already exists.")
    room = OtRoom(**body.model_dump())
    db.add(room)
    await db.flush()
    await audit(db, user, "create_ot_room", None, body.model_dump(mode="json"), {"room_id": str(room.id)}, "Room added")
    await db.commit()
    return ot_svc.room_out(room)


@router.patch("/rooms/{room_id}")
async def update_room(room_id: uuid.UUID, body: RoomPatch, user: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    room = await get_or_404(db, OtRoom, room_id)
    changes = body.model_dump(exclude_none=True)
    merged = RoomPatch(**{**ot_svc.room_out(room), **changes, "capabilities": changes.get("capabilities", room.capabilities)})
    _check_room(merged)
    for k, v in changes.items():
        setattr(room, k, v)
    await audit(db, user, "update_ot_room", None, body.model_dump(mode="json", exclude_none=True), {"room_id": str(room.id)},
                "Room settings changed")
    await db.commit()
    return ot_svc.room_out(room)


# ── Calendar and availability ────────────────────────────────────────────────
@router.get("/schedule")
async def get_schedule(day: date | None = None, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    day = day or ot_svc.now_utc().astimezone(ot_svc.clinic_tz()).date()
    out = await ot_svc.schedule(db, day)
    out["to_schedule"] = await ot_svc.to_schedule(db)
    out["to_confirm"] = await ot_svc.tentative_list(db)
    return out


@router.get("/availability")
async def availability(item_id: str, day: date | None = None, part: str | None = None,
                       count: int = Query(default=3, ge=1, le=12), _: User = Depends(doctor_or_admin),
                       db: AsyncSession = Depends(get_db)):
    item = await get_or_404(db, CatalogueItem, item_id)
    return {"item_id": item.id, "procedure": item.name, "duration_min": item.duration_min,
            "slots": await ot_svc.find_slots(db, item, count=count, day=day, part=part)}


# ── Blocks ───────────────────────────────────────────────────────────────────
@router.get("/blocks")
async def list_blocks(_: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Current and upcoming blocks of rooms, doctors (leave) and machines."""
    blocks = list((await db.execute(select(OtBlock).where(OtBlock.ends_at > ot_svc.now_utc())
                                    .order_by(OtBlock.starts_at).limit(200))).scalars())
    names = {**{r.id: r.name for r in (await db.execute(select(OtRoom))).scalars()},
             **{e.id: e.name for e in (await db.execute(select(OtEquipment))).scalars()},
             **await ot_svc.people_names(db, [k.user_id for k in blocks])}
    return [ot_svc.block_out(k, names) for k in blocks]


@router.post("/blocks", status_code=201)
async def create_block(body: BlockIn, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    blk = await ot_svc.add_block(db, room_id=body.room_id, doctor_id=body.doctor_id, equipment_id=body.equipment_id,
                                 starts_at=body.starts_at, ends_at=body.ends_at, reason=body.reason, user_id=user.id)
    await audit(db, user, "block_ot_resource", None, body.model_dump(mode="json"), {"block_id": str(blk.id)}, body.reason)
    await db.commit()
    return ot_svc.block_out(blk)


@router.delete("/blocks/{block_id}")
async def delete_block(block_id: uuid.UUID, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    blk = await get_or_404(db, OtBlock, block_id)
    await audit(db, user, "unblock_ot_room", None, {"block_id": str(blk.id)}, {"ok": True}, "Block removed")
    await db.delete(blk)
    await db.commit()
    return {"ok": True}


# ── Bookings ─────────────────────────────────────────────────────────────────
async def _detail(db: AsyncSession, b: OtBooking) -> dict:
    return await ot_svc.booking_detail(db, b)


@router.get("/visits/{visit_id}/bookings")
async def visit_bookings(visit_id: uuid.UUID, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    visit = await get_or_404(db, Visit, visit_id)
    return await ot_svc.visit_appointments(db, visit)


@router.get("/tentative")
async def tentative(_: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    return await ot_svc.tentative_list(db)


@router.post("/check")
async def check(body: CheckIn, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Dry run of a booking or a move: every problem found, and whether it can be overridden."""
    item = await get_or_404(db, CatalogueItem, body.item_id)
    visit = None
    if body.booking_id:
        b = await get_or_404(db, OtBooking, body.booking_id)
        visit = await db.get(Visit, b.visit_id)
    plan = await ot_svc.check(db, item=item, room_id=body.room_id, starts_at=body.starts_at, doctor_id=body.doctor_id,
                              equipment_id=body.equipment_id, exclude_booking=body.booking_id, visit=visit)
    people = await ot_svc.people_names(db, [plan.doctor_id])
    eq = await db.get(OtEquipment, plan.equipment_id) if plan.equipment_id else None
    return {"ok": not plan.violations, "needs_override": bool(plan.soft) and not plan.hard,
            "conflicts": [v.out() for v in plan.violations],
            "doctor_id": str(plan.doctor_id) if plan.doctor_id else None, "doctor_name": people.get(plan.doctor_id),
            "equipment_id": str(plan.equipment_id) if plan.equipment_id else None,
            "equipment_name": eq.name if eq else None}


@router.get("/bookings/{booking_id}")
async def get_booking(booking_id: uuid.UUID, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    return await _detail(db, await get_or_404(db, OtBooking, booking_id))


@router.post("/bookings", status_code=201)
async def create_booking(body: BookingIn, request: Request, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Staff bookings are confirmed straight away. Send an Idempotency-Key so a retried request books once."""
    async def do():
        visit = await get_or_404(db, Visit, body.visit_id)
        item = await get_or_404(db, CatalogueItem, body.item_id)
        b = await ot_svc.book(db, visit=visit, item=item, room_id=body.room_id, starts_at=body.starts_at,
                              user_id=user.id, source="staff", doctor_id=body.doctor_id, equipment_id=body.equipment_id,
                              override_reason=body.override_reason, allow_override=True)
        out = await _detail(db, b)
        await audit(db, user, "book_ot_slot", visit.id, body.model_dump(mode="json"),
                    {k: out[k] for k in ("id", "label", "room_name", "doctor_name", "equipment_name", "overridden_rules")},
                    ("Booked by staff, overriding " + ", ".join(b.overridden_rules) + ": " + (b.override_reason or ""))
                    if b.overridden_rules else "Booked by staff")
        await db.commit()
        out["messages"] = await orchestrator.on_ot_changed(db, visit, user)
        return out

    return await run_idempotent(db, request, user.id, do, status_code=201, check_body=True)


@router.patch("/bookings/{booking_id}")
async def reschedule_booking(booking_id: uuid.UUID, body: RescheduleIn, request: Request,
                             user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Move a booking to another time, room, doctor or machine. Send the version you are looking at."""
    async def do():
        before = await get_or_404(db, OtBooking, booking_id)
        old = before.starts_at.isoformat() if before.starts_at else None
        b = await ot_svc.reschedule(db, booking_id, version=body.version, starts_at=body.starts_at, room_id=body.room_id,
                                    doctor_id=body.doctor_id, equipment_id=body.equipment_id, user_id=user.id,
                                    override_reason=body.override_reason, allow_override=True)
        out = await _detail(db, b)
        await audit(db, user, "reschedule_ot_booking", b.visit_id, {**body.model_dump(mode="json"), "from": old},
                    {k: out[k] for k in ("id", "label", "room_name", "doctor_name", "equipment_name", "overridden_rules")},
                    body.override_reason or "Moved by staff")
        await db.commit()
        return out

    return await run_idempotent(db, request, user.id, do, check_body=True)


@router.post("/bookings/{booking_id}/confirm")
async def confirm_booking(booking_id: uuid.UUID, body: VersionIn, user: User = Depends(doctor_or_admin),
                          db: AsyncSession = Depends(get_db)):
    """Confirm a tentative booking (chosen in the chat). The client is texted."""
    b = await ot_svc.confirm(db, booking_id, version=body.version, user_id=user.id)
    out = await _detail(db, b)
    await audit(db, user, "confirm_ot_booking", b.visit_id, {"booking_id": str(b.id)}, {"label": out["label"]},
                "Confirmed by staff")
    client = await db.get(Client, b.client_id)
    try:
        await get_messenger().send("sms", client.mobile,
                                   f"Hi {client.full_name.split()[0]}, your {out['procedure']} on {out['label']} in the "
                                   f"{out['room_name']} is confirmed. See you then.")
        out["notified"] = True
    except Exception:  # noqa: BLE001 - the confirmation stands even if the text fails
        out["notified"] = False
    await db.commit()
    return out


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: uuid.UUID, body: CancelIn, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    b = await ot_svc.locked_booking(db, booking_id, None)
    await ot_svc.cancel(db, b, body.reason, body.version)
    out = await _detail(db, b)
    await audit(db, user, "cancel_ot_booking", b.visit_id, {"booking_id": str(b.id), "reason": body.reason}, {"status": out["status"]},
                body.reason or "Cancelled by staff")
    await db.commit()
    return out


@router.post("/bookings/{booking_id}/stage")
async def set_stage(booking_id: uuid.UUID, body: StageIn, user: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    """Check in, start (needs a confirmed booking and a complete checklist), finish, or mark a no-show."""
    b = await ot_svc.set_stage(db, booking_id, body.stage, version=body.version, at=body.at)
    out = await _detail(db, b)
    await audit(db, user, f"ot_{body.stage}", b.visit_id, body.model_dump(mode="json"), {"stage": b.stage},
                f"Treatment {body.stage.replace('_', ' ')}")
    await db.commit()
    return out


@router.patch("/bookings/{booking_id}/readiness/{key}")
async def update_readiness(booking_id: uuid.UUID, key: str, body: ReadinessIn, user: User = Depends(doctor_or_admin),
                           db: AsyncSession = Depends(get_db)):
    it = await ot_svc.update_readiness(db, booking_id, key, body.status, body.note, user.id)
    b = await db.get(OtBooking, booking_id)
    await audit(db, user, "ot_readiness", b.visit_id, {"booking_id": str(booking_id), "key": key, **body.model_dump()},
                {"status": it.status}, f"Checklist: {it.label} → {it.status.replace('_', ' ')}")
    await db.commit()
    return await _detail(db, b)


# ── Machines, doctors' hours and holidays ────────────────────────────────────
@router.get("/equipment")
async def list_equipment(_: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    return [ot_svc.equipment_out(e) for e in (await db.execute(select(OtEquipment).order_by(OtEquipment.code))).scalars()]


@router.post("/equipment", status_code=201)
async def create_equipment(body: EquipmentIn, user: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    if (await db.execute(select(OtEquipment).where(OtEquipment.code == body.code))).scalar_one_or_none():
        raise HTTPException(409, f"Machine {body.code} already exists.")
    e = OtEquipment(**body.model_dump())
    db.add(e)
    await db.flush()
    await audit(db, user, "create_ot_equipment", None, body.model_dump(), {"id": str(e.id)}, "Machine added")
    await db.commit()
    return ot_svc.equipment_out(e)


@router.patch("/equipment/{equipment_id}")
async def update_equipment(equipment_id: uuid.UUID, body: EquipmentPatch, user: User = Depends(admin_only),
                           db: AsyncSession = Depends(get_db)):
    e = await get_or_404(db, OtEquipment, equipment_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(e, k, v)
    await audit(db, user, "update_ot_equipment", None, changes, {"id": str(e.id)}, "Machine settings changed")
    await db.commit()
    return ot_svc.equipment_out(e)


@router.get("/doctors")
async def list_doctors(_: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    docs = (await db.execute(select(User).where(User.role == "doctor").order_by(User.full_name))).scalars().all()
    hours = (await db.execute(select(DoctorHours).order_by(DoctorHours.weekday, DoctorHours.start_time))).scalars().all()
    return [{"id": str(d.id), "full_name": d.full_name, "active": d.is_active,
             "hours": [{"weekday": h.weekday, "start_time": h.start_time.strftime("%H:%M"),
                        "end_time": h.end_time.strftime("%H:%M")} for h in hours if h.user_id == d.id]} for d in docs]


@router.put("/doctors/{doctor_id}/hours")
async def set_hours(doctor_id: uuid.UUID, body: HoursIn, user: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    doc = await get_or_404(db, User, doctor_id)
    if doc.role != "doctor":
        raise HTTPException(422, "Working hours are kept for doctors only.")
    for h in body.hours:
        if h.end_time <= h.start_time:
            raise HTTPException(422, "Each shift must end after it starts.")
    for h in (await db.execute(select(DoctorHours).where(DoctorHours.user_id == doc.id))).scalars():
        await db.delete(h)
    for h in body.hours:
        db.add(DoctorHours(user_id=doc.id, **h.model_dump()))
    await audit(db, user, "set_doctor_hours", None, {"doctor_id": str(doc.id), **body.model_dump(mode="json")},
                {"rows": len(body.hours)}, f"Working hours set for {doc.full_name}")
    await db.commit()
    return {"ok": True, "rows": len(body.hours)}


@router.get("/holidays")
async def list_holidays(_: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ClinicHoliday).order_by(ClinicHoliday.day))).scalars()
    return [{"day": h.day.isoformat(), "name": h.name} for h in rows]


@router.post("/holidays", status_code=201)
async def add_holiday(body: HolidayIn, user: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    """Adds a closed day. Existing bookings on it stay, and are listed so the front desk can move them."""
    if await db.get(ClinicHoliday, body.day):
        raise HTTPException(409, "That day is already a holiday.")
    db.add(ClinicHoliday(day=body.day, name=body.name))
    zone = ot_svc.clinic_tz()
    lo, hi = datetime.combine(body.day, time(0), zone), datetime.combine(body.day, time(23, 59, 59), zone)
    affected = (await db.execute(select(OtBooking).where(OtBooking.status.in_(ot_svc.HOLD), OtBooking.starts_at >= lo,
                                                         OtBooking.starts_at <= hi))).scalars().all()
    await audit(db, user, "add_holiday", None, body.model_dump(mode="json"), {"affected": len(affected)}, body.name)
    await db.commit()
    return {"day": body.day.isoformat(), "name": body.name, "affected_bookings": len(affected),
            "affected": [{"id": str(b.id), "label": ot_svc.slot_label(b.starts_at)} for b in affected]}


@router.delete("/holidays/{day}")
async def delete_holiday(day: date, user: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    h = await db.get(ClinicHoliday, day)
    if h is None:
        raise HTTPException(404, "Not a holiday.")
    await db.delete(h)
    await audit(db, user, "remove_holiday", None, {"day": day.isoformat()}, {"ok": True}, h.name)
    await db.commit()
    return {"ok": True}


# ── Reports ──────────────────────────────────────────────────────────────────
def _range(from_: date | None, to: date | None) -> tuple[date, date]:
    today = ot_svc.now_utc().astimezone(ot_svc.clinic_tz()).date()
    to = to or today
    return (from_ or to - timedelta(days=29)), to


@router.get("/reports")
async def get_report(from_: date | None = Query(default=None, alias="from"), to: date | None = None,
                     room_id: uuid.UUID | None = None, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    first, last = _range(from_, to)
    return await ot_reports.report(db, first, last, room_id)


@router.get("/reports.csv")
async def get_report_csv(from_: date | None = Query(default=None, alias="from"), to: date | None = None,
                         room_id: uuid.UUID | None = None, _: User = Depends(doctor_or_admin),
                         db: AsyncSession = Depends(get_db)):
    first, last = _range(from_, to)
    rep = await ot_reports.report(db, first, last, room_id)
    return Response(ot_reports.to_csv(rep), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="treatment-rooms-{first}-to-{last}.csv"'})
