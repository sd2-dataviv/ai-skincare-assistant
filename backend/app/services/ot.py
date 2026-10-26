"""Operation theatre (procedure room) availability and booking.

A clinic treatment in the catalogue names the kind of room it needs (`room_type`), its chair time (`duration_min`),
the machine it needs (`equipment_type`, optional) and whether a doctor must perform it (`needs_doctor`).

One rule check (`evaluate`) decides whether a treatment can start in a room at a time. It is used both to offer
slots in the chat (only slots with no problem at all) and to book from the staff calendar:
- hard rules, never overridable: room unsuitable or closed down, time in the past, room/doctor/machine already
  booked or blocked, no working machine or no free doctor;
- soft rules, which a doctor or admin may override with a written reason: outside the room's opening hours, a clinic
  holiday, the doctor off shift.
Unnamed doctors and machines are picked automatically: free first, on-shift first, the doctor who approved the plan
first. Booking locks the room row and re-checks; the database also refuses overlapping holds of the same room,
doctor or machine, so a race between two bookings can't double book.

Bookings made in the chat are *tentative* until staff confirm them; both tentative and confirmed bookings hold their
resources. Each booking carries a pre-treatment checklist (readiness) and a version number: every change bumps it,
and a change sent with an old version is refused, so two people can't overwrite each other.
"""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (Approval, CatalogueItem, Client, ClinicHoliday, DoctorHours, OtBlock, OtBooking, OtEquipment,
                        OtReadinessItem, OtRoom, Photo, Prescription, User, Visit)
from app.services import DomainError

PARTS = ("morning", "afternoon", "evening")
HOLD = ("tentative", "confirmed")  # statuses that hold a room, doctor and machine
SOFT_RULES = {"outside_hours", "holiday", "doctor_off_shift"}
READY_DONE = ("done", "not_applicable")

DEFAULT_ROOMS = [
    {"code": "OT-1", "name": "Laser Suite", "capabilities": ["laser", "led"]},
    {"code": "OT-2", "name": "Procedure Room", "capabilities": ["minor_procedure", "peel"]},
    {"code": "OT-3", "name": "Facial Room", "capabilities": ["facial", "peel", "led"]},
]
DEFAULT_EQUIPMENT = [
    {"code": "EQ-1", "name": "Q-switched Nd:YAG laser", "equipment_type": "qswitch_laser", "buffer_minutes": 10},
    {"code": "EQ-2", "name": "LED light panel", "equipment_type": "led_panel", "buffer_minutes": 0},
    {"code": "EQ-3", "name": "Dermapen", "equipment_type": "microneedling_pen", "buffer_minutes": 30},
    {"code": "EQ-4", "name": "HydraFacial machine", "equipment_type": "hydrafacial_machine", "buffer_minutes": 15},
]
# (key, label, mandatory, room types it applies to; None = all)
READINESS_TEMPLATE = [
    ("consent", "Treatment consent form signed", True, None),
    ("patch_test", "Patch test done at least 48 hours before", True, {"laser", "peel"}),
    ("photos", "Before photos on file", True, None),
    ("health_recheck", "Medication, pregnancy and allergies re-checked today", True, None),
    ("skin_prep", "Make-up removed and skin cleansed", True, None),
    ("aftercare", "Aftercare explained to the client", False, None),
]


# ── Time helpers ─────────────────────────────────────────────────────────────
def clinic_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().clinic_timezone)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def part_of_day(t: time) -> str:
    return "morning" if t < time(12) else "afternoon" if t < time(16) else "evening"


def clock(dt: datetime) -> str:
    local = dt.astimezone(clinic_tz())
    h = local.hour % 12 or 12
    return f"{h}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"


def day_label(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def slot_label(starts_at: datetime) -> str:
    """'Mon 19 Oct, 10:00 am' in clinic time. Also what the client taps, so it must be unique within an offer."""
    local = starts_at.astimezone(clinic_tz())
    return f"{day_label(local.date())}, {clock(starts_at)}"


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise DomainError("naive_datetime", "Times must include a timezone offset.", 422)
    return dt.astimezone(timezone.utc)


def _overlaps(intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(a < end and start < b for a, b in intervals)


# ── Plan → procedures ────────────────────────────────────────────────────────
async def plan_procedures(db: AsyncSession, rx: Prescription | None) -> list[dict]:
    """Treatments in the plan that need a room, in plan order."""
    if rx is None:
        return []
    ids = [i["catalogue_id"] for i in rx.items if i.get("kind") == "service"]
    if not ids:
        return []
    rows = {c.id: c for c in (await db.execute(select(CatalogueItem).where(CatalogueItem.id.in_(ids)))).scalars()}
    out = []
    for i in rx.items:
        c = rows.get(i["catalogue_id"])
        if c and c.room_type:
            out.append({"item_id": c.id, "name": c.name, "room_type": c.room_type,
                        "duration_min": c.duration_min or 30, "sessions": int(i.get("sessions") or c.default_sessions or 1)})
    return out


async def visit_bookings(db: AsyncSession, visit_id: uuid.UUID, include_cancelled: bool = False) -> list[OtBooking]:
    q = select(OtBooking).where(OtBooking.visit_id == visit_id)
    if not include_cancelled:
        q = q.where(OtBooking.status != "cancelled")
    return list((await db.execute(q.order_by(OtBooking.created_at))).scalars())


def pending(procedures: list[dict], bookings: list[OtBooking]) -> list[dict]:
    """First sessions still to be booked or deferred (later sessions are booked at each visit)."""
    decided = {b.catalogue_item_id for b in bookings if b.session_index == 1 and b.status in (*HOLD, "deferred")}
    return [p for p in procedures if p["item_id"] not in decided]


# ── Seed data ────────────────────────────────────────────────────────────────
async def seed_rooms(db: AsyncSession) -> int:
    n = 0
    for r in DEFAULT_ROOMS:
        exists = (await db.execute(select(OtRoom).where(OtRoom.code == r["code"]))).scalar_one_or_none()
        if exists:
            continue
        db.add(OtRoom(code=r["code"], name=r["name"], capabilities=r["capabilities"], open_time=time(10),
                      close_time=time(19), open_days=[0, 1, 2, 3, 4, 5], buffer_minutes=15, active=True))
        n += 1
    await db.flush()
    return n


async def seed_equipment(db: AsyncSession) -> int:
    n = 0
    for e in DEFAULT_EQUIPMENT:
        if (await db.execute(select(OtEquipment).where(OtEquipment.code == e["code"]))).scalar_one_or_none():
            continue
        db.add(OtEquipment(**e, active=True))
        n += 1
    await db.flush()
    return n


async def seed_doctor_hours(db: AsyncSession) -> int:
    """Doctors with no working hours at all get Mon–Sat 10:00–19:00, so they can be booked straight away."""
    doctors = (await db.execute(select(User).where(User.role == "doctor", User.is_active.is_(True)))).scalars().all()
    n = 0
    for d in doctors:
        if (await db.execute(select(DoctorHours.id).where(DoctorHours.user_id == d.id).limit(1))).first():
            continue
        for wd in range(6):
            db.add(DoctorHours(user_id=d.id, weekday=wd, start_time=time(10), end_time=time(19)))
        n += 1
    await db.flush()
    return n


async def rooms_for(db: AsyncSession, room_type: str) -> list[OtRoom]:
    return list((await db.execute(select(OtRoom).where(OtRoom.active.is_(True), OtRoom.capabilities.contains([room_type]))
                                  .order_by(OtRoom.code))).scalars())


def _fits_hours(room: OtRoom, start_local: datetime, end_local: datetime) -> bool:
    return (start_local.weekday() in (room.open_days or []) and start_local.date() == end_local.date()
            and room.open_time <= start_local.time() and end_local.time() <= room.close_time)


# ── Rule check ───────────────────────────────────────────────────────────────
@dataclass
class Violation:
    code: str
    message: str
    soft: bool

    def out(self) -> dict:
        return {"code": self.code, "message": self.message, "overridable": self.soft}


@dataclass
class Plan:
    violations: list[Violation] = field(default_factory=list)
    doctor_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    ends_at: datetime | None = None
    clear_until: datetime | None = None
    equipment_clear_until: datetime | None = None

    @property
    def hard(self) -> list[Violation]:
        return [v for v in self.violations if not v.soft]

    @property
    def soft(self) -> list[Violation]:
        return [v for v in self.violations if v.soft]


@dataclass
class Snapshot:
    """Everything the rule check needs for a time window, read once."""
    room_busy: dict[uuid.UUID, list[tuple[datetime, datetime]]]
    doctors: list[User]
    doctor_busy: dict[uuid.UUID, list[tuple[datetime, datetime]]]
    doctor_hours: dict[uuid.UUID, dict[int, list[tuple[time, time]]]]
    equipment: dict[str, list[OtEquipment]]  # active machines by type
    equipment_busy: dict[uuid.UUID, list[tuple[datetime, datetime]]]
    holidays: dict[date, str]


async def load_snapshot(db: AsyncSession, start: datetime, end: datetime,
                        exclude_booking: uuid.UUID | None = None) -> Snapshot:
    """Busy times of every room, doctor and machine overlapping [start, end) (padded for buffers)."""
    lo, hi = start - timedelta(hours=3), end + timedelta(hours=3)
    room_busy: dict = {}
    doctor_busy: dict = {}
    equip_busy: dict = {}
    bq = select(OtBooking).where(OtBooking.status.in_(HOLD), OtBooking.starts_at < hi,
                                 OtBooking.clear_until > lo)
    if exclude_booking:
        bq = bq.where(OtBooking.id != exclude_booking)
    for b in (await db.execute(bq)).scalars():
        room_busy.setdefault(b.room_id, []).append((b.starts_at, b.clear_until))
        if b.doctor_id:
            doctor_busy.setdefault(b.doctor_id, []).append((b.starts_at, b.ends_at))
        if b.equipment_id:
            equip_busy.setdefault(b.equipment_id, []).append((b.starts_at, b.equipment_clear_until or b.ends_at))
    for k in (await db.execute(select(OtBlock).where(OtBlock.starts_at < hi, OtBlock.ends_at > lo))).scalars():
        target = room_busy if k.room_id else doctor_busy if k.user_id else equip_busy
        target.setdefault(k.room_id or k.user_id or k.equipment_id, []).append((k.starts_at, k.ends_at))
    doctors = list((await db.execute(select(User).where(User.role == "doctor", User.is_active.is_(True))
                                     .order_by(User.full_name))).scalars())
    hours: dict = {}
    for h in (await db.execute(select(DoctorHours))).scalars():
        hours.setdefault(h.user_id, {}).setdefault(h.weekday, []).append((h.start_time, h.end_time))
    equipment: dict = {}
    for e in (await db.execute(select(OtEquipment).where(OtEquipment.active.is_(True)).order_by(OtEquipment.code))).scalars():
        equipment.setdefault(e.equipment_type, []).append(e)
    zone = clinic_tz()
    d0, d1 = lo.astimezone(zone).date(), hi.astimezone(zone).date()
    holidays = {h.day: h.name for h in (await db.execute(select(ClinicHoliday).where(ClinicHoliday.day >= d0,
                                                                                     ClinicHoliday.day <= d1))).scalars()}
    return Snapshot(room_busy, doctors, doctor_busy, hours, equipment, equip_busy, holidays)


def on_shift(snap: Snapshot, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool:
    zone = clinic_tz()
    s, e = start.astimezone(zone), end.astimezone(zone)
    if s.date() != e.date():
        return False
    return any(t0 <= s.time() and e.time() <= t1 for t0, t1 in snap.doctor_hours.get(doctor_id, {}).get(s.weekday(), []))


def evaluate(snap: Snapshot, item: CatalogueItem, room: OtRoom, start: datetime, *, now: datetime,
             doctor_id: uuid.UUID | None = None, equipment_id: uuid.UUID | None = None,
             preferred_doctor: uuid.UUID | None = None) -> Plan:
    dur = timedelta(minutes=item.duration_min or 30)
    end = start + dur
    plan = Plan(ends_at=end, clear_until=end + timedelta(minutes=room.buffer_minutes))
    v = plan.violations
    zone = clinic_tz()
    if not room.active:
        v.append(Violation("room_inactive", f"{room.name} is out of use.", False))
    if item.room_type not in (room.capabilities or []):
        v.append(Violation("room_unsuitable", f"{room.name} isn't set up for {item.name}.", False))
    if start < now:
        v.append(Violation("in_the_past", "That time has already passed.", False))
    if _overlaps(snap.room_busy.get(room.id, []), start, plan.clear_until):
        v.append(Violation("room_busy", f"{room.name} is booked or blocked then (including cleaning time).", False))
    if not _fits_hours(room, start.astimezone(zone), end.astimezone(zone)):
        v.append(Violation("outside_hours", f"{room.name} is closed at that time.", True))
    hol = snap.holidays.get(start.astimezone(zone).date())
    if hol:
        v.append(Violation("holiday", f"The clinic is closed for {hol}.", True))

    if item.equipment_type:
        machines = snap.equipment.get(item.equipment_type, [])
        nice = item.equipment_type.replace("_", " ")

        def free(e: OtEquipment) -> bool:
            return not _overlaps(snap.equipment_busy.get(e.id, []), start, end + timedelta(minutes=e.buffer_minutes))

        if equipment_id:
            chosen = next((e for e in machines if e.id == equipment_id), None)
            if chosen is None:
                v.append(Violation("equipment_unsuitable", f"That machine can't be used for {item.name}.", False))
            elif not free(chosen):
                v.append(Violation("equipment_busy", f"{chosen.name} is in use or under maintenance then.", False))
        else:
            chosen = next((e for e in machines if free(e)), None)
            if not machines:
                v.append(Violation("no_equipment", f"No working {nice} is available for {item.name}.", False))
            elif chosen is None:
                v.append(Violation("equipment_busy", f"Every {nice} is in use or under maintenance then.", False))
        if chosen is not None:
            plan.equipment_id = chosen.id
            plan.equipment_clear_until = end + timedelta(minutes=chosen.buffer_minutes)

    if item.needs_doctor or doctor_id:
        def free_doc(d: uuid.UUID) -> bool:
            return not _overlaps(snap.doctor_busy.get(d, []), start, end)

        if doctor_id:
            if doctor_id not in {d.id for d in snap.doctors}:
                v.append(Violation("doctor_unknown", "Choose an active doctor.", False))
            else:
                plan.doctor_id = doctor_id
                if not free_doc(doctor_id):
                    v.append(Violation("doctor_busy", "That doctor has another treatment or is on leave then.", False))
                elif not on_shift(snap, doctor_id, start, end):
                    v.append(Violation("doctor_off_shift", "That doctor isn't working at that time.", True))
        else:
            free_docs = [d.id for d in snap.doctors if free_doc(d.id)]
            free_docs.sort(key=lambda d: d != preferred_doctor)  # the approving doctor first
            on = [d for d in free_docs if on_shift(snap, d, start, end)]
            if on:
                plan.doctor_id = on[0]
            elif free_docs:
                plan.doctor_id = free_docs[0]
                v.append(Violation("doctor_off_shift", "No doctor is on shift then.", True))
            else:
                v.append(Violation("no_doctor", f"No doctor is free then to perform {item.name}.", False))
    return plan


async def _approving_doctor(db: AsyncSession, visit: Visit) -> uuid.UUID | None:
    return (await db.execute(select(Approval.doctor_id).where(Approval.visit_id == visit.id,
                                                             Approval.status.in_(["approved", "edited"]))
                             .order_by(Approval.decided_at.desc().nulls_last()).limit(1))).scalar_one_or_none()


# ── Availability ─────────────────────────────────────────────────────────────
async def find_slots(db: AsyncSession, item: CatalogueItem, *, count: int | None = None, after: datetime | None = None,
                     day: date | None = None, part: str | None = None, now: datetime | None = None,
                     horizon_days: int | None = None, preferred_doctor: uuid.UUID | None = None) -> list[dict]:
    """Earliest slots with no problem at all (no overrides), for the chat and the staff 'find a time' search.

    Without a day/part filter, offers are spread out (at most one per morning/afternoon/evening of a day) so the
    client gets a real choice; with a filter, offered times are at least an hour apart.
    """
    s = get_settings()
    count = count or s.ot_offer_count
    if not item.room_type:
        raise DomainError("no_room_needed", f"{item.name} doesn't need a treatment room.", 422)
    if part and part not in PARTS:
        raise DomainError("invalid_part", "part must be morning, afternoon or evening.", 422)
    rooms = await rooms_for(db, item.room_type)
    if not rooms:
        return []
    zone = clinic_tz()
    current = now or now_utc()
    floor = current + timedelta(minutes=s.ot_min_lead_minutes)
    first_day = floor.astimezone(zone).date()
    if after:
        first_day = max(first_day, after.astimezone(zone).date())
    if day:
        if day < first_day:
            return []
        first_day = last_day = day
    else:
        last_day = floor.astimezone(zone).date() + timedelta(days=horizon_days or s.ot_horizon_days)
    window = (datetime.combine(first_day, time(0), zone), datetime.combine(last_day + timedelta(days=1), time(0), zone))
    snap = await load_snapshot(db, *window)
    dur = timedelta(minutes=item.duration_min or 30)
    step = timedelta(minutes=s.ot_slot_step_minutes)
    spread = day is None and part is None
    out: list[dict] = []
    buckets: set[tuple[date, str]] = set()
    d = first_day
    while d <= last_day and len(out) < count:
        open_rooms = [r for r in rooms if d.weekday() in (r.open_days or [])]
        if open_rooms and d not in snap.holidays:
            t = datetime.combine(d, min(r.open_time for r in open_rooms), zone)
            day_end = datetime.combine(d, max(r.close_time for r in open_rooms), zone)
            while t + dur <= day_end and len(out) < count:
                pod = part_of_day(t.time())
                ok_time = t >= floor and (after is None or t > after) and (part is None or pod == part)
                if spread:
                    ok_time = ok_time and (d, pod) not in buckets
                elif out:
                    ok_time = ok_time and t - datetime.fromisoformat(out[-1]["starts_at"]) >= timedelta(hours=1)
                if ok_time:
                    start = t.astimezone(timezone.utc)
                    for r in open_rooms:
                        plan = evaluate(snap, item, r, start, now=current, preferred_doctor=preferred_doctor)
                        if plan.violations:
                            continue
                        out.append({"room_id": str(r.id), "room_code": r.code, "room_name": r.name,
                                    "starts_at": start.isoformat(), "ends_at": (start + dur).isoformat(),
                                    "label": slot_label(start),
                                    "doctor_id": str(plan.doctor_id) if plan.doctor_id else None,
                                    "equipment_id": str(plan.equipment_id) if plan.equipment_id else None})
                        buckets.add((d, pod))
                        break
                t += step
        d += timedelta(days=1)
    return out


async def check(db: AsyncSession, *, item: CatalogueItem, room_id: uuid.UUID, starts_at: datetime,
                doctor_id: uuid.UUID | None = None, equipment_id: uuid.UUID | None = None,
                exclude_booking: uuid.UUID | None = None, visit: Visit | None = None) -> Plan:
    """Dry run: what would stop (or need an override for) this booking."""
    starts_at = _aware(starts_at)
    room = await db.get(OtRoom, room_id)
    if room is None:
        raise DomainError("room_not_found", "Room not found.", 404)
    dur = timedelta(minutes=item.duration_min or 30)
    snap = await load_snapshot(db, starts_at, starts_at + dur, exclude_booking=exclude_booking)
    pref = await _approving_doctor(db, visit) if visit else None
    return evaluate(snap, item, room, starts_at, now=now_utc(), doctor_id=doctor_id, equipment_id=equipment_id,
                    preferred_doctor=pref)


def _refuse(plan: Plan, allow_override: bool, override_reason: str | None) -> None:
    """Raise if the plan has a hard problem, or soft problems without an accepted override."""
    conflicts = [x.out() for x in plan.violations]
    if plan.hard:
        first = plan.hard[0]
        status = 409 if first.code in ("room_busy", "doctor_busy", "equipment_busy", "no_doctor") else 422
        raise DomainError(first.code, " ".join(x.message for x in plan.hard), status,
                          {"conflicts": conflicts, "needs_override": False})
    if plan.soft and not (allow_override and (override_reason or "").strip()):
        raise DomainError("needs_override", " ".join(x.message for x in plan.soft)
                          + (" Give a reason to book anyway." if allow_override else ""), 409,
                          {"conflicts": conflicts, "needs_override": allow_override})


# ── Booking ──────────────────────────────────────────────────────────────────
async def _approved_procs(db: AsyncSession, visit: Visit) -> dict[str, dict]:
    rx = (await db.execute(select(Prescription).where(Prescription.visit_id == visit.id))).scalar_one_or_none()
    return {p["item_id"]: p for p in await plan_procedures(db, rx)} if rx and rx.status == "approved" else {}


async def book(db: AsyncSession, *, visit: Visit, item: CatalogueItem, room_id: uuid.UUID, starts_at: datetime,
               user_id: uuid.UUID | None, source: str, session_index: int = 1, now: datetime | None = None,
               status: str | None = None, doctor_id: uuid.UUID | None = None, equipment_id: uuid.UUID | None = None,
               override_reason: str | None = None, allow_override: bool = False) -> OtBooking:
    """Book a treatment. Chat bookings (source 'ai') are tentative; staff bookings are confirmed."""
    starts_at = _aware(starts_at)
    status = status or ("tentative" if source == "ai" else "confirmed")
    if not item.room_type:
        raise DomainError("no_room_needed", f"{item.name} doesn't need a treatment room.", 422)
    procs = await _approved_procs(db, visit)
    if item.id not in procs:
        raise DomainError("not_in_plan", f"{item.name} isn't a treatment in this visit's approved plan.", 422)
    room = (await db.execute(select(OtRoom).where(OtRoom.id == room_id).with_for_update())).scalar_one_or_none()
    if room is None:
        raise DomainError("room_not_found", "That room doesn't exist.", 404)
    existing = (await db.execute(select(OtBooking).where(
        OtBooking.visit_id == visit.id, OtBooking.catalogue_item_id == item.id, OtBooking.session_index == session_index,
        OtBooking.status.in_([*HOLD, "deferred"])).with_for_update())).scalar_one_or_none()
    if existing is not None and existing.status in HOLD:
        raise DomainError("already_booked", f"{item.name} is already booked for this visit.", 409)
    dur = timedelta(minutes=item.duration_min or 30)
    snap = await load_snapshot(db, starts_at, starts_at + dur)
    plan = evaluate(snap, item, room, starts_at, now=now or now_utc(), doctor_id=doctor_id, equipment_id=equipment_id,
                    preferred_doctor=await _approving_doctor(db, visit))
    if source == "ai" and plan.violations:  # the chat only books clean slots; no overrides
        if any(v.code in ("room_busy", "doctor_busy", "equipment_busy", "no_doctor") for v in plan.violations):
            raise DomainError("slot_taken", "Sorry, that time has just been taken.", 409)
        first = (plan.hard or plan.soft)[0]
        raise DomainError(first.code, first.message, 422)
    _refuse(plan, allow_override, override_reason)
    try:
        async with db.begin_nested():
            if existing is None:
                existing = OtBooking(visit_id=visit.id, client_id=visit.client_id, catalogue_item_id=item.id,
                                     session_index=session_index, sessions_total=procs[item.id]["sessions"], version=0)
                db.add(existing)
            existing.status = status
            existing.room_id = room.id
            existing.starts_at = starts_at
            existing.ends_at = plan.ends_at
            existing.clear_until = plan.clear_until
            existing.doctor_id = plan.doctor_id
            existing.equipment_id = plan.equipment_id
            existing.equipment_clear_until = plan.equipment_clear_until
            existing.source = source
            existing.created_by_user_id = user_id
            existing.stage = "scheduled"
            existing.confirmed_at = now_utc() if status == "confirmed" else None
            existing.confirmed_by_user_id = user_id if status == "confirmed" else None
            existing.override_reason = (override_reason or "").strip()[:300] or None if plan.soft else None
            existing.overridden_rules = [x.code for x in plan.soft]
            existing.version = (existing.version or 0) + 1
            await db.flush()
    except IntegrityError:
        raise DomainError("slot_taken", "Sorry, that time has just been taken.", 409)
    await ensure_checklist(db, existing, item, visit)
    return existing


async def defer(db: AsyncSession, *, visit: Visit, item: CatalogueItem, user_id: uuid.UUID | None, source: str) -> OtBooking:
    procs = await _approved_procs(db, visit)
    if item.id not in procs:
        raise DomainError("not_in_plan", f"{item.name} isn't a treatment in this visit's approved plan.", 422)
    existing = (await db.execute(select(OtBooking).where(
        OtBooking.visit_id == visit.id, OtBooking.catalogue_item_id == item.id, OtBooking.session_index == 1,
        OtBooking.status.in_([*HOLD, "deferred"])))).scalar_one_or_none()
    if existing:
        return existing
    b = OtBooking(visit_id=visit.id, client_id=visit.client_id, catalogue_item_id=item.id, session_index=1,
                  sessions_total=procs[item.id]["sessions"], status="deferred", source=source, created_by_user_id=user_id)
    db.add(b)
    await db.flush()
    return b


async def locked_booking(db: AsyncSession, booking_id: uuid.UUID, version: int | None) -> OtBooking:
    """The booking row, locked; refuses a change made against an old copy (someone else changed it first)."""
    b = (await db.execute(select(OtBooking).where(OtBooking.id == booking_id).with_for_update()
                          .execution_options(populate_existing=True))).scalar_one_or_none()
    if b is None:
        raise DomainError("not_found", "Booking not found.", 404)
    if version is not None and version != b.version:
        raise DomainError("stale_version", "Someone else changed this booking just now. Refresh and try again.", 409,
                          {"current_version": b.version})
    return b


async def reschedule(db: AsyncSession, booking_id: uuid.UUID, *, version: int, starts_at: datetime,
                     room_id: uuid.UUID | None, doctor_id: uuid.UUID | None, equipment_id: uuid.UUID | None,
                     user_id: uuid.UUID | None, override_reason: str | None, allow_override: bool) -> OtBooking:
    """Move a booking. Re-checked like a new booking (ignoring itself); if anything fails, the original stands."""
    starts_at = _aware(starts_at)
    b = await locked_booking(db, booking_id, version)
    if b.status not in HOLD:
        raise DomainError("not_booked", "Only a booked treatment can be moved.", 409)
    if b.stage != "scheduled":
        raise DomainError("already_started", "This treatment has already started or the client has checked in.", 409)
    item = await db.get(CatalogueItem, b.catalogue_item_id)
    room = (await db.execute(select(OtRoom).where(OtRoom.id == (room_id or b.room_id)).with_for_update())).scalar_one_or_none()
    if room is None:
        raise DomainError("room_not_found", "Room not found.", 404)
    visit = await db.get(Visit, b.visit_id)
    dur = timedelta(minutes=item.duration_min or 30)
    snap = await load_snapshot(db, starts_at, starts_at + dur, exclude_booking=b.id)
    plan = evaluate(snap, item, room, starts_at, now=now_utc(), doctor_id=doctor_id, equipment_id=equipment_id,
                    preferred_doctor=b.doctor_id or await _approving_doctor(db, visit))
    _refuse(plan, allow_override, override_reason)
    try:
        async with db.begin_nested():
            b.room_id, b.starts_at, b.ends_at, b.clear_until = room.id, starts_at, plan.ends_at, plan.clear_until
            b.doctor_id, b.equipment_id, b.equipment_clear_until = plan.doctor_id, plan.equipment_id, plan.equipment_clear_until
            b.override_reason = (override_reason or "").strip()[:300] or None if plan.soft else None
            b.overridden_rules = [x.code for x in plan.soft]
            b.version += 1
            await db.flush()
    except IntegrityError:
        raise DomainError("slot_taken", "Sorry, that time has just been taken. The booking wasn't moved.", 409)
    return b


async def confirm(db: AsyncSession, booking_id: uuid.UUID, *, version: int, user_id: uuid.UUID) -> OtBooking:
    b = await locked_booking(db, booking_id, version)
    if b.status == "confirmed":
        raise DomainError("already_confirmed", "This booking is already confirmed.", 409)
    if b.status != "tentative":
        raise DomainError("not_tentative", "Only a tentative booking can be confirmed.", 409)
    if b.starts_at and b.starts_at < now_utc():
        raise DomainError("in_the_past", "That time has already passed; move the booking first.", 409)
    b.status = "confirmed"
    b.confirmed_at = now_utc()
    b.confirmed_by_user_id = user_id
    b.version += 1
    await db.flush()
    return b


async def cancel(db: AsyncSession, booking: OtBooking, reason: str | None, version: int | None = None) -> OtBooking:
    if version is not None and version != booking.version:
        raise DomainError("stale_version", "Someone else changed this booking just now. Refresh and try again.", 409,
                          {"current_version": booking.version})
    if booking.status == "cancelled":
        return booking
    if booking.stage in ("in_progress", "completed"):
        raise DomainError("already_started", "A treatment that has started can't be cancelled.", 409)
    booking.status = "cancelled"
    booking.cancelled_at = now_utc()
    booking.cancel_reason = (reason or "")[:200] or None
    booking.version += 1
    await db.flush()
    return booking


# ── Readiness checklist and the day itself ───────────────────────────────────
async def ensure_checklist(db: AsyncSession, b: OtBooking, item: CatalogueItem, visit: Visit) -> list[OtReadinessItem]:
    have = {i.key: i for i in (await db.execute(select(OtReadinessItem).where(OtReadinessItem.booking_id == b.id))).scalars()}
    photos = (await db.execute(select(Photo).where(Photo.visit_id == visit.id, Photo.accepted.is_(True)))).scalars().all()
    for pos, (key, label, mandatory, applies) in enumerate(READINESS_TEMPLATE):
        if key in have or (applies is not None and item.room_type not in applies):
            continue
        it = OtReadinessItem(booking_id=b.id, key=key, label=label, mandatory=mandatory, status="pending", position=pos)
        if key == "photos" and len({p.pose for p in photos}) >= 3:
            it.status = "done"
            it.note = f"Consultation photos from {visit.created_at.astimezone(clinic_tz()):%d %b %Y}"
            it.updated_at = now_utc()
        db.add(it)
    await db.flush()
    return await checklist(db, b.id)


async def checklist(db: AsyncSession, booking_id: uuid.UUID) -> list[OtReadinessItem]:
    return list((await db.execute(select(OtReadinessItem).where(OtReadinessItem.booking_id == booking_id)
                                  .order_by(OtReadinessItem.position))).scalars())


def readiness_summary(items: list[OtReadinessItem]) -> dict:
    open_mandatory = [i for i in items if i.mandatory and i.status not in READY_DONE]
    blocked = [i for i in items if i.status == "blocked"]
    return {"complete": bool(items) and not open_mandatory and not blocked, "open_mandatory": len(open_mandatory),
            "blocked": len(blocked), "total": len(items), "done": sum(i.status in READY_DONE for i in items)}


def readiness_out(i: OtReadinessItem, people: dict | None = None) -> dict:
    return {"key": i.key, "label": i.label, "mandatory": i.mandatory, "status": i.status, "note": i.note,
            "updated_at": i.updated_at.isoformat() if i.updated_at else None,
            "updated_by": (people or {}).get(i.updated_by_user_id)}


async def update_readiness(db: AsyncSession, booking_id: uuid.UUID, key: str, status: str, note: str | None,
                           user_id: uuid.UUID) -> OtReadinessItem:
    if status not in ("pending", "done", "not_applicable", "blocked"):
        raise DomainError("invalid_status", "Status must be pending, done, not_applicable or blocked.", 422)
    note = (note or "").strip() or None
    if status in ("not_applicable", "blocked") and not note:
        raise DomainError("note_required", "Say why (a short note) when an item is not applicable or blocked.", 422)
    b = await locked_booking(db, booking_id, None)
    if b.status not in HOLD or b.stage in ("in_progress", "completed", "no_show"):
        raise DomainError("checklist_closed", "The checklist can't change once the treatment has started or ended.", 409)
    it = (await db.execute(select(OtReadinessItem).where(OtReadinessItem.booking_id == b.id, OtReadinessItem.key == key))
          ).scalar_one_or_none()
    if it is None:
        raise DomainError("not_found", "No such checklist item for this booking.", 404)
    it.status, it.note, it.updated_by_user_id, it.updated_at = status, note, user_id, now_utc()
    b.version += 1  # the booking shown on screen is now out of date
    await db.flush()
    return it


STAGE_FLOW = {"checked_in": ("scheduled",), "in_progress": ("checked_in",), "completed": ("in_progress",),
              "no_show": ("scheduled", "checked_in")}


async def set_stage(db: AsyncSession, booking_id: uuid.UUID, stage: str, *, version: int,
                    at: datetime | None = None) -> OtBooking:
    """Check in → start → finish (or no-show). Starting needs a confirmed booking and a complete checklist."""
    if stage not in STAGE_FLOW:
        raise DomainError("invalid_stage", "Stage must be checked_in, in_progress, completed or no_show.", 422)
    b = await locked_booking(db, booking_id, version)
    if b.status not in HOLD:
        raise DomainError("not_booked", "This booking is cancelled or not booked.", 409)
    if b.stage not in STAGE_FLOW[stage]:
        raise DomainError("bad_transition", f"Can't go from {b.stage.replace('_', ' ')} to {stage.replace('_', ' ')}.", 409)
    if b.status == "tentative" and stage in ("checked_in", "in_progress"):
        raise DomainError("not_confirmed", "Confirm the booking before checking the client in.", 409)
    now = now_utc()
    when = _aware(at) if at else now
    if when > now + timedelta(minutes=1):
        raise DomainError("in_the_future", "That time is in the future.", 422)
    if when < now - timedelta(hours=12):
        raise DomainError("too_far_back", "Times can be back-dated by at most 12 hours.", 422)
    previous = {"in_progress": b.checked_in_at, "completed": b.started_at}.get(stage)
    if previous and when < previous:
        raise DomainError("before_previous", "That time is before the previous step.", 422)
    if stage == "in_progress":
        summ = readiness_summary(await checklist(db, b.id))
        if not summ["complete"]:
            raise DomainError("not_ready", f"The checklist isn't complete: {summ['open_mandatory']} required item(s) "
                              f"open{', ' + str(summ['blocked']) + ' blocked' if summ['blocked'] else ''}.", 409,
                              {"readiness": summ})
    b.stage = stage
    if stage == "checked_in":
        b.checked_in_at = when
    elif stage == "in_progress":
        b.started_at = when
    elif stage == "completed":
        b.finished_at = when
    b.version += 1
    await db.flush()
    return b


# ── Blocks ───────────────────────────────────────────────────────────────────
async def add_block(db: AsyncSession, *, starts_at: datetime, ends_at: datetime, reason: str, user_id: uuid.UUID | None,
                    room_id: uuid.UUID | None = None, doctor_id: uuid.UUID | None = None,
                    equipment_id: uuid.UUID | None = None) -> OtBlock:
    """Block a room (maintenance), a doctor (leave) or a machine (service). Refused if it would clash with bookings."""
    starts_at, ends_at = _aware(starts_at), _aware(ends_at)
    if sum(x is not None for x in (room_id, doctor_id, equipment_id)) != 1:
        raise DomainError("one_target", "Block exactly one room, doctor or machine.", 422)
    if ends_at <= starts_at:
        raise DomainError("invalid_range", "The block must end after it starts.", 422)
    if ends_at - starts_at > timedelta(days=31):
        raise DomainError("invalid_range", "Blocks can be at most 31 days long.", 422)
    if room_id:
        target = (await db.execute(select(OtRoom).where(OtRoom.id == room_id).with_for_update())).scalar_one_or_none()
        q = select(OtBooking).where(OtBooking.room_id == room_id, OtBooking.starts_at < ends_at,
                                    OtBooking.clear_until > starts_at)
    elif doctor_id:
        target = (await db.execute(select(User).where(User.id == doctor_id, User.role == "doctor"))).scalar_one_or_none()
        q = select(OtBooking).where(OtBooking.doctor_id == doctor_id, OtBooking.starts_at < ends_at,
                                    OtBooking.ends_at > starts_at)
    else:
        target = await db.get(OtEquipment, equipment_id)
        q = select(OtBooking).where(OtBooking.equipment_id == equipment_id, OtBooking.starts_at < ends_at,
                                    OtBooking.equipment_clear_until > starts_at)
    if target is None:
        raise DomainError("not_found", "Room, doctor or machine not found.", 404)
    name = getattr(target, "name", None) or getattr(target, "full_name", "It")
    clashes = (await db.execute(q.where(OtBooking.status.in_(HOLD)).order_by(OtBooking.starts_at))).scalars().all()
    if clashes:
        when = ", ".join(slot_label(b.starts_at) for b in clashes[:5])
        raise DomainError("conflicts_with_bookings",
                          f"{name} has {len(clashes)} booking(s) in that time ({when}). Move or cancel them first.", 409)
    blk = OtBlock(room_id=room_id, user_id=doctor_id, equipment_id=equipment_id, starts_at=starts_at, ends_at=ends_at,
                  reason=(reason or "Unavailable")[:200], created_by_user_id=user_id)
    db.add(blk)
    await db.flush()
    return blk


# ── Views ────────────────────────────────────────────────────────────────────
def booking_out(b: OtBooking, item: CatalogueItem | None, room: OtRoom | None, client: Client | None = None,
                people: dict | None = None, equipment: OtEquipment | None = None,
                readiness: list[OtReadinessItem] | None = None) -> dict:
    people = people or {}
    out = {"id": str(b.id), "visit_id": str(b.visit_id), "item_id": b.catalogue_item_id,
           "procedure": item.name if item else b.catalogue_item_id, "status": b.status,
           "session_index": b.session_index, "sessions_total": b.sessions_total, "source": b.source,
           "room_id": str(room.id) if room else None, "room_name": room.name if room else None,
           "room_code": room.code if room else None,
           "starts_at": b.starts_at.isoformat() if b.starts_at else None,
           "ends_at": b.ends_at.isoformat() if b.ends_at else None,
           "clear_until": b.clear_until.isoformat() if b.clear_until else None,
           "label": slot_label(b.starts_at) if b.starts_at else None,
           "tentative": b.status == "tentative",
           "doctor_id": str(b.doctor_id) if b.doctor_id else None, "doctor_name": people.get(b.doctor_id),
           "equipment_id": str(b.equipment_id) if b.equipment_id else None,
           "equipment_name": equipment.name if equipment else None,
           "stage": b.stage or "scheduled", "version": b.version,
           "confirmed_at": b.confirmed_at.isoformat() if b.confirmed_at else None,
           "confirmed_by": people.get(b.confirmed_by_user_id),
           "checked_in_at": b.checked_in_at.isoformat() if b.checked_in_at else None,
           "started_at": b.started_at.isoformat() if b.started_at else None,
           "finished_at": b.finished_at.isoformat() if b.finished_at else None,
           "override_reason": b.override_reason, "overridden_rules": list(b.overridden_rules or []),
           "cancel_reason": b.cancel_reason}
    if client is not None:
        out["client_name"] = client.full_name
    if readiness is not None:
        out["readiness"] = readiness_summary(readiness)
    return out


async def people_names(db: AsyncSession, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.full_name for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}


async def booking_detail(db: AsyncSession, b: OtBooking) -> dict:
    item = await db.get(CatalogueItem, b.catalogue_item_id)
    room = await db.get(OtRoom, b.room_id) if b.room_id else None
    eq = await db.get(OtEquipment, b.equipment_id) if b.equipment_id else None
    client = await db.get(Client, b.client_id)
    items = await checklist(db, b.id)
    people = await people_names(db, [b.doctor_id, b.confirmed_by_user_id, *[i.updated_by_user_id for i in items]])
    out = booking_out(b, item, room, client, people, eq, items)
    out["checklist"] = [readiness_out(i, people) for i in items]
    out["needs_doctor"] = bool(item and item.needs_doctor)
    out["equipment_type"] = item.equipment_type if item else None
    out["duration_min"] = item.duration_min if item else None
    return out


async def visit_appointments(db: AsyncSession, visit: Visit) -> list[dict]:
    out = []
    bookings = await visit_bookings(db, visit.id)
    people = await people_names(db, [b.doctor_id for b in bookings])
    for b in bookings:
        item = await db.get(CatalogueItem, b.catalogue_item_id)
        room = await db.get(OtRoom, b.room_id) if b.room_id else None
        eq = await db.get(OtEquipment, b.equipment_id) if b.equipment_id else None
        out.append(booking_out(b, item, room, people=people, equipment=eq))
    return out


def room_out(r: OtRoom) -> dict:
    return {"id": str(r.id), "code": r.code, "name": r.name, "capabilities": list(r.capabilities or []),
            "open_time": r.open_time.strftime("%H:%M"), "close_time": r.close_time.strftime("%H:%M"),
            "open_days": list(r.open_days or []), "buffer_minutes": r.buffer_minutes, "active": r.active}


def equipment_out(e: OtEquipment) -> dict:
    return {"id": str(e.id), "code": e.code, "name": e.name, "equipment_type": e.equipment_type,
            "buffer_minutes": e.buffer_minutes, "active": e.active}


def block_out(k: OtBlock, names: dict | None = None) -> dict:
    target = "room" if k.room_id else "doctor" if k.user_id else "equipment"
    tid = k.room_id or k.user_id or k.equipment_id
    return {"id": str(k.id), "target": target, "target_id": str(tid), "target_name": (names or {}).get(tid),
            "room_id": str(k.room_id) if k.room_id else None, "starts_at": k.starts_at.isoformat(),
            "ends_at": k.ends_at.isoformat(), "reason": k.reason}


async def schedule(db: AsyncSession, day: date) -> dict:
    zone = clinic_tz()
    start, end = datetime.combine(day, time(0), zone), datetime.combine(day + timedelta(days=1), time(0), zone)
    rooms = list((await db.execute(select(OtRoom).where(OtRoom.active.is_(True)).order_by(OtRoom.code))).scalars())
    rows = (await db.execute(select(OtBooking, CatalogueItem, Client)
                             .join(CatalogueItem, CatalogueItem.id == OtBooking.catalogue_item_id)
                             .join(Client, Client.id == OtBooking.client_id)
                             .where(OtBooking.status.in_(HOLD), OtBooking.starts_at < end, OtBooking.clear_until > start)
                             .order_by(OtBooking.starts_at))).all()
    ids = [b.id for b, _, _ in rows]
    items_by: dict = {}
    if ids:
        for it in (await db.execute(select(OtReadinessItem).where(OtReadinessItem.booking_id.in_(ids)))).scalars():
            items_by.setdefault(it.booking_id, []).append(it)
    equipment = {e.id: e for e in (await db.execute(select(OtEquipment))).scalars()}
    people = await people_names(db, [b.doctor_id for b, _, _ in rows])
    blocks = list((await db.execute(select(OtBlock).where(OtBlock.starts_at < end, OtBlock.ends_at > start)
                                    .order_by(OtBlock.starts_at))).scalars())
    names = {**{r.id: r.name for r in rooms}, **{e.id: e.name for e in equipment.values()},
             **await people_names(db, [k.user_id for k in blocks])}
    out_rooms = []
    for r in rooms:
        out_rooms.append({**room_out(r), "open_today": day.weekday() in (r.open_days or []),
                          "bookings": [booking_out(b, i, r, c, people, equipment.get(b.equipment_id), items_by.get(b.id, []))
                                       for b, i, c in rows if b.room_id == r.id],
                          "blocks": [block_out(k, names) for k in blocks if k.room_id == r.id]})
    holiday = await db.get(ClinicHoliday, day)
    return {"day": day.isoformat(), "timezone": get_settings().clinic_timezone, "rooms": out_rooms,
            "holiday": holiday.name if holiday else None,
            "other_blocks": [block_out(k, names) for k in blocks if not k.room_id],
            "tentative": sum(1 for b, _, _ in rows if b.status == "tentative")}


async def tentative_list(db: AsyncSession) -> list[dict]:
    """Upcoming bookings waiting for staff to confirm them (chosen by clients in the chat)."""
    rows = (await db.execute(select(OtBooking, CatalogueItem, Client, OtRoom)
                             .join(CatalogueItem, CatalogueItem.id == OtBooking.catalogue_item_id)
                             .join(Client, Client.id == OtBooking.client_id)
                             .join(OtRoom, OtRoom.id == OtBooking.room_id)
                             .where(OtBooking.status == "tentative", OtBooking.starts_at > now_utc())
                             .order_by(OtBooking.starts_at))).all()
    people = await people_names(db, [b.doctor_id for b, *_ in rows])
    return [booking_out(b, i, r, c, people) for b, i, c, r in rows]


async def to_schedule(db: AsyncSession, days_back: int = 90) -> list[dict]:
    """Approved procedures with no booked first session: deferred by the client, cancelled, or never offered."""
    since = now_utc() - timedelta(days=days_back)
    rows = (await db.execute(select(Visit, Client, Prescription).join(Client, Client.id == Visit.client_id)
                             .join(Prescription, Prescription.visit_id == Visit.id)
                             .where(Prescription.status == "approved", Visit.created_at >= since)
                             .order_by(Visit.created_at))).all()
    out = []
    for visit, client, rx in rows:
        procs = await plan_procedures(db, rx)
        if not procs:
            continue
        bookings = await visit_bookings(db, visit.id)
        held = {b.catalogue_item_id for b in bookings if b.status in HOLD and b.session_index == 1}
        deferred = {b.catalogue_item_id for b in bookings if b.status == "deferred"}
        for p in procs:
            if p["item_id"] in held:
                continue
            if visit.status not in ("approved", "completed"):
                continue
            if (visit.status == "approved" and p["item_id"] not in deferred
                    and visit.created_at > now_utc() - timedelta(hours=6)):
                continue  # the client is probably still choosing a slot on the clinic device
            out.append({"visit_id": str(visit.id), "client_name": client.full_name, "item_id": p["item_id"],
                        "procedure": p["name"], "duration_min": p["duration_min"], "sessions": p["sessions"],
                        "deferred": p["item_id"] in deferred, "visit_date": visit.created_at.date().isoformat()})
    return out
