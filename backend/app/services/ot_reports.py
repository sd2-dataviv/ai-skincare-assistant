"""Treatment-room reports: utilisation, turnover, punctuality and outcomes, from recorded data.

Scope: bookings whose planned start falls on the chosen clinic-local dates. Every rate comes with its numerator and
denominator, and is None (shown as "—") when there is nothing to measure, never 0 %.

- Available minutes: each room's opening hours on its open days, minus clinic holidays and room blocks.
- Booked utilisation: planned treatment minutes of tentative and confirmed bookings ÷ available minutes.
- Actual utilisation: recorded in-room minutes (start → finish) ÷ available minutes.
- Turnover: in one room on one day, the gap from one treatment's finish to the next one's start (recorded times),
  ignoring gaps over 2 hours (those are idle time, not turnover).
- On-time start: started no more than 10 minutes after the planned start.
- Overrun: finished after the planned end.
"""
import csv
import io
import statistics
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CatalogueItem, ClinicHoliday, OtBlock, OtBooking, OtEquipment, OtRoom, User
from app.services import DomainError
from app.services.ot import HOLD, clinic_tz

ON_TIME_GRACE = timedelta(minutes=10)
MAX_TURNOVER = timedelta(hours=2)


def _rate(num: float, den: float) -> dict:
    return {"value": round(num / den * 100, 1) if den else None, "num": round(num, 1), "den": round(den, 1)}


def _minutes(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 60)


def _clip(a: datetime, b: datetime, lo: datetime, hi: datetime) -> float:
    return _minutes(max(a, lo), min(b, hi)) if a < hi and lo < b else 0.0


async def report(db: AsyncSession, first: date, last: date, room_id: uuid.UUID | None = None) -> dict:
    if last < first:
        raise DomainError("invalid_range", "The end date is before the start date.", 422)
    if (last - first).days > 366:
        raise DomainError("invalid_range", "Reports cover at most a year.", 422)
    zone = clinic_tz()
    lo, hi = datetime.combine(first, time(0), zone), datetime.combine(last + timedelta(days=1), time(0), zone)
    rq = select(OtRoom).order_by(OtRoom.code)
    if room_id:
        rq = rq.where(OtRoom.id == room_id)
    rooms = list((await db.execute(rq)).scalars())
    holidays = {h.day for h in (await db.execute(select(ClinicHoliday).where(ClinicHoliday.day >= first,
                                                                             ClinicHoliday.day <= last))).scalars()}
    blocks = list((await db.execute(select(OtBlock).where(OtBlock.starts_at < hi, OtBlock.ends_at > lo))).scalars())
    bq = select(OtBooking).where(OtBooking.starts_at >= lo, OtBooking.starts_at < hi, OtBooking.room_id.is_not(None))
    if room_id:
        bq = bq.where(OtBooking.room_id == room_id)
    bookings = list((await db.execute(bq.order_by(OtBooking.starts_at))).scalars())
    items = {i.id: i for i in (await db.execute(select(CatalogueItem))).scalars()}
    equipment = {e.id: e for e in (await db.execute(select(OtEquipment))).scalars()}
    doctors = {u.id: u.full_name for u in (await db.execute(select(User).where(User.role == "doctor"))).scalars()}

    def stats(bs: list[OtBooking], available: float) -> dict:
        live = [b for b in bs if b.status in HOLD]
        booked = sum(_minutes(b.starts_at, b.ends_at) for b in live)
        done = [b for b in live if b.started_at and b.finished_at]
        actual = sum(_minutes(b.started_at, b.finished_at) for b in done)
        started = [b for b in live if b.started_at]
        on_time = [b for b in started if b.started_at <= b.starts_at + ON_TIME_GRACE]
        overruns = [_minutes(b.ends_at, b.finished_at) for b in done if b.finished_at > b.ends_at]
        gaps: list[float] = []
        by_day: dict = {}
        for b in done:
            by_day.setdefault((b.room_id, b.starts_at.astimezone(zone).date()), []).append(b)
        for seq in by_day.values():
            seq.sort(key=lambda x: x.started_at)
            for prev, nxt in zip(seq, seq[1:]):
                gap = nxt.started_at - prev.finished_at
                if timedelta(0) <= gap <= MAX_TURNOVER:
                    gaps.append(gap.total_seconds() / 60)
        cancelled = [b for b in bs if b.status == "cancelled"]
        reasons: dict[str, int] = {}
        for b in cancelled:
            reasons[b.cancel_reason or "No reason given"] = reasons.get(b.cancel_reason or "No reason given", 0) + 1
        return {
            "available_min": round(available), "booked_min": round(booked), "actual_min": round(actual),
            "booked_utilisation": _rate(booked, available), "actual_utilisation": _rate(actual, available),
            "bookings": len(live), "tentative": sum(b.status == "tentative" for b in live),
            "confirmed": sum(b.status == "confirmed" for b in live),
            "completed": sum(b.stage == "completed" for b in live), "no_show": sum(b.stage == "no_show" for b in live),
            "cancelled": len(cancelled), "cancel_reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
            "no_show_rate": _rate(sum(b.stage == "no_show" for b in live),
                                  sum(b.stage in ("completed", "no_show") for b in live)),
            "on_time_start": _rate(len(on_time), len(started)),
            "overruns": len(overruns), "avg_overrun_min": round(statistics.mean(overruns), 1) if overruns else None,
            "turnover": {"count": len(gaps), "avg_min": round(statistics.mean(gaps), 1) if gaps else None,
                         "median_min": round(statistics.median(gaps), 1) if gaps else None},
            "overrides": sum(1 for b in live if b.overridden_rules),
        }

    room_rows = []
    total_available = 0.0
    for r in rooms:
        available = 0.0
        d = first
        while d <= last:
            if d.weekday() in (r.open_days or []) and d not in holidays:
                o, c = datetime.combine(d, r.open_time, zone), datetime.combine(d, r.close_time, zone)
                avail = _minutes(o, c)
                for k in blocks:
                    if k.room_id == r.id:
                        avail -= _clip(k.starts_at, k.ends_at, o, c)
                available += max(0.0, avail)
            d += timedelta(days=1)
        total_available += available
        room_rows.append({"room_id": str(r.id), "code": r.code, "name": r.name, "buffer_minutes": r.buffer_minutes,
                          "active": r.active, **stats([b for b in bookings if b.room_id == r.id], available)})

    live = [b for b in bookings if b.status in HOLD]
    by_procedure: dict[str, dict] = {}
    for b in live:
        name = items[b.catalogue_item_id].name if b.catalogue_item_id in items else b.catalogue_item_id
        row = by_procedure.setdefault(name, {"procedure": name, "bookings": 0, "minutes": 0})
        row["bookings"] += 1
        row["minutes"] += round(_minutes(b.starts_at, b.ends_at))
    by_equipment: dict = {}
    for b in live:
        if b.equipment_id:
            e = equipment.get(b.equipment_id)
            row = by_equipment.setdefault(b.equipment_id, {"equipment": e.name if e else "?", "bookings": 0, "minutes": 0})
            row["bookings"] += 1
            row["minutes"] += round(_minutes(b.starts_at, b.equipment_clear_until or b.ends_at))
    by_doctor: dict = {}
    for b in live:
        if b.doctor_id:
            row = by_doctor.setdefault(b.doctor_id, {"doctor": doctors.get(b.doctor_id, "?"), "bookings": 0, "minutes": 0})
            row["bookings"] += 1
            row["minutes"] += round(_minutes(b.starts_at, b.ends_at))
    eq_down = 0.0
    for k in blocks:
        if k.equipment_id:
            eq_down += _clip(k.starts_at, k.ends_at, lo, hi)
    return {
        "from": first.isoformat(), "to": last.isoformat(), "timezone": str(zone),
        "total": stats(bookings, total_available), "rooms": room_rows,
        "by_procedure": sorted(by_procedure.values(), key=lambda x: -x["minutes"]),
        "by_equipment": sorted(by_equipment.values(), key=lambda x: -x["minutes"]),
        "by_doctor": sorted(by_doctor.values(), key=lambda x: -x["minutes"]),
        "equipment_downtime_min": round(eq_down), "holidays": sorted(d.isoformat() for d in holidays),
    }


def to_csv(rep: dict) -> str:
    """One row per room plus a total row. No client names or other personal data."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["room", "available_min", "booked_min", "booked_utilisation_pct", "actual_min", "actual_utilisation_pct",
                "bookings", "tentative", "confirmed", "completed", "no_show", "cancelled", "on_time_start_pct",
                "turnover_count", "turnover_avg_min", "turnover_median_min", "overruns", "avg_overrun_min", "overrides"])

    def row(name: str, s: dict) -> list:
        return [name, s["available_min"], s["booked_min"], s["booked_utilisation"]["value"], s["actual_min"],
                s["actual_utilisation"]["value"], s["bookings"], s["tentative"], s["confirmed"], s["completed"],
                s["no_show"], s["cancelled"], s["on_time_start"]["value"], s["turnover"]["count"],
                s["turnover"]["avg_min"], s["turnover"]["median_min"], s["overruns"], s["avg_overrun_min"], s["overrides"]]

    for r in rep["rooms"]:
        w.writerow(row(f"{r['code']} {r['name']}", r))
    w.writerow(row("All rooms", rep["total"]))
    return buf.getvalue()
