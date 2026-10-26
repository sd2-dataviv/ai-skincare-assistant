"""Treatment rooms, part 2: doctors' shifts and machines, holidays, tentative bookings that staff confirm, the
pre-treatment checklist and the day itself, safe retries and concurrent edits, and the utilisation/turnover report."""
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import sessionmaker
from app.integrations.messaging import OUTBOX
from app.models import CatalogueItem, OtBooking, OtEquipment, OtRoom, User
from app.services import DomainError
from app.services import ot
from tests.test_ot import _visit_with_plan

IST = ZoneInfo("Asia/Kolkata")
_today = datetime.now(IST).date()
MON = _today + timedelta(days=(7 - _today.weekday()) % 7 + 7)  # a Monday 1–2 weeks ahead
TUE = MON + timedelta(days=1)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), IST)


@pytest.fixture(autouse=True)
async def only_demo_doctor():
    """Other tests add doctor accounts (with no working hours); keep only the demo doctor active here."""
    async with sessionmaker()() as db:
        others = (await db.execute(select(User).where(User.role == "doctor", User.email != "doctor@clinic.local",
                                                      User.is_active.is_(True)))).scalars().all()
        for u in others:
            u.is_active = False
        await db.commit()
        ids = [u.id for u in others]
    yield
    async with sessionmaker()() as db:
        for uid_ in ids:
            (await db.get(User, uid_)).is_active = True
        await db.commit()


async def _room(db, code: str) -> OtRoom:
    return (await db.execute(select(OtRoom).where(OtRoom.code == code))).scalar_one()


async def _doctor(db) -> User:
    return (await db.execute(select(User).where(User.email == "doctor@clinic.local"))).scalar_one()


async def _book(db, items: list[str], item: str, room: str, start: datetime, **kw) -> OtBooking:
    v = await _visit_with_plan(db, items)
    return await ot.book(db, visit=v, item=await db.get(CatalogueItem, item), room_id=(await _room(db, room)).id,
                         starts_at=start, user_id=None, source=kw.pop("source", "staff"), **kw)


# ── Doctors and machines ─────────────────────────────────────────────────────
async def test_one_machine_serves_two_rooms_but_one_treatment_at_a_time():
    async with sessionmaker()() as db:
        led = await db.get(CatalogueItem, "svc-led-therapy")
        b = await _book(db, ["svc-led-therapy"], "svc-led-therapy", "OT-1", at(MON, 10))
        assert b.equipment_id and b.doctor_id is None  # LED needs the panel, not a doctor
        # The Facial Room is free at 10:00, but the only LED panel isn't
        with pytest.raises(DomainError) as e:
            await _book(db, ["svc-led-therapy"], "svc-led-therapy", "OT-3", at(MON, 10, 15))
        assert e.value.code == "equipment_busy" and e.value.extra["conflicts"][0]["overridable"] is False
        slots = await ot.find_slots(db, led, day=MON, part="morning", count=6)
        assert slots and all(datetime.fromisoformat(s["starts_at"]) >= at(MON, 10, 30) for s in slots)
        # Out of service → no LED treatments anywhere
        panel = (await db.execute(select(OtEquipment).where(OtEquipment.equipment_type == "led_panel"))).scalar_one()
        panel.active = False
        await db.flush()
        with pytest.raises(DomainError) as e:
            await _book(db, ["svc-led-therapy"], "svc-led-therapy", "OT-3", at(TUE, 12))
        assert e.value.code == "no_equipment"
        assert await ot.find_slots(db, led, day=TUE) == []
        await db.rollback()


async def test_machine_maintenance_and_cool_down():
    async with sessionmaker()() as db:
        laser = await db.get(CatalogueItem, "svc-qswitch-toning")
        machine = (await db.execute(select(OtEquipment).where(OtEquipment.equipment_type == "qswitch_laser"))).scalar_one()
        await ot.add_block(db, equipment_id=machine.id, starts_at=at(MON, 10), ends_at=at(MON, 12), reason="Service visit",
                           user_id=None)
        first = (await ot.find_slots(db, laser, day=MON))[0]
        assert datetime.fromisoformat(first["starts_at"]) >= at(MON, 12)
        await db.rollback()


async def test_a_doctor_cant_be_in_two_rooms_at_once():
    async with sessionmaker()() as db:
        doc = await _doctor(db)
        laser = await _book(db, ["svc-qswitch-toning"], "svc-qswitch-toning", "OT-1", at(MON, 10))
        assert laser.doctor_id == doc.id  # the only doctor, picked automatically
        with pytest.raises(DomainError) as e:  # a peel in another room at the same time needs the same doctor
            await _book(db, ["svc-glycolic-peel"], "svc-glycolic-peel", "OT-2", at(MON, 10, 15))
        assert e.value.code == "no_doctor"
        # A facial needs no doctor, so it can run alongside
        await _book(db, ["svc-hydrafacial"], "svc-hydrafacial", "OT-3", at(MON, 10))
        await db.commit()
        # Even writing straight to the table can't double-book the doctor
        v = await _visit_with_plan(db, ["svc-glycolic-peel"])
        db.add(OtBooking(visit_id=v.id, client_id=v.client_id, catalogue_item_id="svc-glycolic-peel", status="tentative",
                         room_id=(await _room(db, "OT-2")).id, doctor_id=doc.id, starts_at=at(MON, 10, 15),
                         ends_at=at(MON, 11), clear_until=at(MON, 11, 15), source="staff"))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_off_shift_and_holidays_need_a_written_override_but_leave_never_does(client, admin):
    async with sessionmaker()() as db:
        doc = await _doctor(db)
        laser = await db.get(CatalogueItem, "svc-qswitch-toning")
        room = await _room(db, "OT-1")
        doc_id = doc.id
    # Mondays: the doctor only works until noon
    hours = [{"weekday": d, "start_time": "10:00", "end_time": "19:00"} for d in range(1, 6)]
    r = await client.put(f"/api/ot/doctors/{doc_id}/hours", json={"hours": hours + [
        {"weekday": 0, "start_time": "10:00", "end_time": "12:00"}]}, headers=admin)
    assert r.status_code == 200
    try:
        async with sessionmaker()() as db:
            slots = await ot.find_slots(db, laser, day=MON, part="afternoon")
            assert slots == []  # the AI never offers off-shift times
            with pytest.raises(DomainError) as e:
                await _book(db, ["svc-qswitch-toning"], "svc-qswitch-toning", "OT-1", at(MON, 15), allow_override=True)
            assert e.value.code == "needs_override" and e.value.extra["needs_override"] is True
            assert [c["code"] for c in e.value.extra["conflicts"]] == ["doctor_off_shift"]
            b = await _book(db, ["svc-qswitch-toning"], "svc-qswitch-toning", "OT-1", at(MON, 15), allow_override=True,
                            override_reason="Dr Rao agreed to stay late")
            assert b.overridden_rules == ["doctor_off_shift"] and b.override_reason == "Dr Rao agreed to stay late"
            # Leave is a hard block: no reason opens it
            await ot.add_block(db, doctor_id=doc_id, starts_at=at(TUE, 0), ends_at=at(TUE, 23), reason="Leave", user_id=None)
            with pytest.raises(DomainError) as e:
                await _book(db, ["svc-qswitch-toning"], "svc-qswitch-toning", "OT-1", at(TUE, 11), allow_override=True,
                            override_reason="please")
            assert e.value.code == "no_doctor"
            await db.commit()

        # A clinic holiday: no slots offered; staff can still book with a reason; the API lists affected bookings
        r = await client.post("/api/ot/holidays", json={"day": MON.isoformat(), "name": "Diwali"}, headers=admin)
        assert r.status_code == 201 and r.json()["affected_bookings"] == 1
        async with sessionmaker()() as db:
            assert await ot.find_slots(db, await db.get(CatalogueItem, "svc-hydrafacial"), day=MON) == []
        async with sessionmaker()() as db:
            v = await _visit_with_plan(db, ["svc-hydrafacial"])
            body = {"visit_id": str(v.id), "item_id": "svc-hydrafacial", "room_id": str((await _room(db, "OT-3")).id),
                    "starts_at": at(MON, 11).isoformat()}
            await db.commit()
        r = await client.post("/api/ot/bookings", json=body, headers=admin)
        assert r.status_code == 409 and r.json()["code"] == "needs_override"
        assert r.json()["conflicts"][0]["code"] == "holiday" and r.json()["needs_override"]
        r = await client.post("/api/ot/bookings", json={**body, "override_reason": "Client only free that day"},
                              headers=admin)
        assert r.status_code == 201 and r.json()["overridden_rules"] == ["holiday"]
        sched = (await client.get("/api/ot/schedule", params={"day": MON.isoformat()}, headers=admin)).json()
        assert sched["holiday"] == "Diwali"
    finally:
        await client.put(f"/api/ot/doctors/{doc_id}/hours", json={"hours": [
            {"weekday": d, "start_time": "10:00", "end_time": "19:00"} for d in range(6)]}, headers=admin)


# ── Tentative bookings, confirmation and concurrent edits ────────────────────
async def test_chat_bookings_are_tentative_until_staff_confirm(client, admin, doctor):
    async with sessionmaker()() as db:
        b = await _book(db, ["svc-hydrafacial"], "svc-hydrafacial", "OT-3", at(MON, 11), source="ai")
        await db.commit()
        bid, version = str(b.id), b.version
    assert b.status == "tentative" and b.confirmed_at is None
    todo = (await client.get("/api/ot/tentative", headers=admin)).json()
    assert [t["id"] for t in todo] == [bid] and todo[0]["tentative"]
    sched = (await client.get("/api/ot/schedule", params={"day": MON.isoformat()}, headers=admin)).json()
    assert sched["tentative"] == 1 and [t["id"] for t in sched["to_confirm"]] == [bid]

    # Someone else changed it since this screen loaded → refused
    OUTBOX.sent.clear()
    r = await client.post(f"/api/ot/bookings/{bid}/confirm", json={"version": version + 5}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "stale_version" and r.json()["current_version"] == version
    r = await client.post(f"/api/ot/bookings/{bid}/confirm", json={"version": version}, headers=doctor)
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["confirmed_by"]
    assert r.json()["notified"] and "is confirmed" in OUTBOX.sent[-1]["body"]
    r = await client.post(f"/api/ot/bookings/{bid}/confirm", json={"version": r.json()["version"]}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "already_confirmed"
    assert (await client.get("/api/ot/tentative", headers=admin)).json() == []


async def test_reschedule_checks_like_a_new_booking_and_keeps_the_original_on_failure(client, admin):
    async with sessionmaker()() as db:
        a = await _book(db, ["svc-hydrafacial"], "svc-hydrafacial", "OT-3", at(MON, 11))
        b = await _book(db, ["svc-hydrafacial"], "svc-hydrafacial", "OT-3", at(MON, 14))
        await db.commit()
        aid, bid, bv = str(a.id), str(b.id), b.version
    # Moving B onto A's time fails and leaves B where it was
    r = await client.patch(f"/api/ot/bookings/{bid}", json={"version": bv, "starts_at": at(MON, 11, 30).isoformat()},
                           headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "room_busy"
    assert (await client.get(f"/api/ot/bookings/{bid}", headers=admin)).json()["label"].endswith("2:00 pm")
    # A dry run explains before trying
    chk = (await client.post("/api/ot/check", json={"item_id": "svc-hydrafacial", "room_id": (
        await client.get(f"/api/ot/bookings/{bid}", headers=admin)).json()["room_id"], "starts_at": at(MON, 11).isoformat(),
        "booking_id": bid}, headers=admin)).json()
    assert not chk["ok"] and chk["conflicts"][0]["code"] == "room_busy"
    # A good move works and bumps the version; the old version can't be used again
    r = await client.patch(f"/api/ot/bookings/{bid}", json={"version": bv, "starts_at": at(MON, 16).isoformat()},
                           headers=admin)
    assert r.status_code == 200 and r.json()["label"].endswith("4:00 pm") and r.json()["version"] == bv + 1
    r = await client.patch(f"/api/ot/bookings/{bid}", json={"version": bv, "starts_at": at(MON, 17).isoformat()},
                           headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "stale_version"
    # Cancelling with an old version is refused too
    r = await client.post(f"/api/ot/bookings/{aid}/cancel", json={"reason": "x", "version": 99}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "stale_version"


async def test_a_retried_booking_request_books_once(client, admin):
    async with sessionmaker()() as db:
        v = await _visit_with_plan(db, ["svc-hydrafacial"])
        room = await _room(db, "OT-3")
        await db.commit()
    body = {"visit_id": str(v.id), "item_id": "svc-hydrafacial", "room_id": str(room.id), "starts_at": at(MON, 12).isoformat()}
    key = {"Idempotency-Key": uuid.uuid4().hex}
    r1 = await client.post("/api/ot/bookings", json=body, headers={**admin, **key})
    r2 = await client.post("/api/ot/bookings", json=body, headers={**admin, **key})
    assert r1.status_code == 201 and r2.status_code in (200, 201) and r1.json()["id"] == r2.json()["id"]
    r3 = await client.post("/api/ot/bookings", json={**body, "starts_at": at(MON, 15).isoformat()}, headers={**admin, **key})
    assert r3.status_code == 422  # same key, different details
    async with sessionmaker()() as db:
        n = (await db.execute(select(OtBooking).where(OtBooking.visit_id == v.id))).scalars().all()
        assert len(n) == 1


# ── Checklist, the day, and the report ───────────────────────────────────────
async def test_checklist_gates_the_start_and_the_report_uses_recorded_times(client, admin):
    async with sessionmaker()() as db:
        a = await _book(db, ["svc-qswitch-toning"], "svc-qswitch-toning", "OT-1", at(MON, 10), source="ai")
        b = await _book(db, ["svc-carbon-laser"], "svc-carbon-laser", "OT-1", at(MON, 11))
        await db.commit()
        aid, bid = str(a.id), str(b.id)
    d = (await client.get(f"/api/ot/bookings/{aid}", headers=admin)).json()
    keys = [i["key"] for i in d["checklist"]]
    assert keys == ["consent", "patch_test", "photos", "health_recheck", "skin_prep", "aftercare"]
    assert d["readiness"]["complete"] is False and d["needs_doctor"] and d["equipment_name"]

    # A tentative booking can't be started; confirm first
    r = await client.post(f"/api/ot/bookings/{aid}/stage", json={"stage": "checked_in", "version": d["version"]}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "not_confirmed"
    d = (await client.post(f"/api/ot/bookings/{aid}/confirm", json={"version": d["version"]}, headers=admin)).json()
    now = datetime.now(IST)
    d = (await client.post(f"/api/ot/bookings/{aid}/stage", json={"stage": "checked_in", "version": d["version"],
                                                                  "at": (now - timedelta(minutes=100)).isoformat()},
                           headers=admin)).json()
    r = await client.post(f"/api/ot/bookings/{aid}/stage", json={"stage": "in_progress", "version": d["version"]}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "not_ready" and r.json()["readiness"]["open_mandatory"] == 5

    r = await client.patch(f"/api/ot/bookings/{aid}/readiness/patch_test", json={"status": "not_applicable"}, headers=admin)
    assert r.status_code == 422  # say why
    for key in ("consent", "photos", "health_recheck", "skin_prep"):
        d = (await client.patch(f"/api/ot/bookings/{aid}/readiness/{key}", json={"status": "done"}, headers=admin)).json()
    d = (await client.patch(f"/api/ot/bookings/{aid}/readiness/patch_test",
                            json={"status": "not_applicable", "note": "Had this laser last month, no reaction"},
                            headers=admin)).json()
    assert d["readiness"]["complete"] and d["checklist"][1]["updated_by"] == "Clinic Admin"
    now = datetime.now(IST)
    for stage, when in (("in_progress", now - timedelta(minutes=95)), ("completed", now - timedelta(minutes=60))):
        r = await client.post(f"/api/ot/bookings/{aid}/stage", json={"stage": stage, "version": d["version"],
                                                                     "at": when.isoformat()}, headers=admin)
        assert r.status_code == 200, r.text
        d = r.json()
    assert d["stage"] == "completed" and d["started_at"] and d["finished_at"]
    r = await client.patch(f"/api/ot/bookings/{aid}/readiness/aftercare", json={"status": "done"}, headers=admin)
    assert r.status_code == 409  # closed once done

    # The second laser: checklist done, started 15 min after the first finished
    e = (await client.get(f"/api/ot/bookings/{bid}", headers=admin)).json()
    for i in e["checklist"]:
        if i["mandatory"]:
            e = (await client.patch(f"/api/ot/bookings/{bid}/readiness/{i['key']}", json={"status": "done"},
                                    headers=admin)).json()
    for stage, when in (("checked_in", now - timedelta(minutes=50)), ("in_progress", now - timedelta(minutes=45)),
                        ("completed", now - timedelta(minutes=10))):
        e = (await client.post(f"/api/ot/bookings/{bid}/stage", json={"stage": stage, "version": e["version"],
                                                                      "at": when.isoformat()}, headers=admin)).json()
    r = await client.post(f"/api/ot/bookings/{bid}/stage", json={"stage": "completed", "version": e["version"]}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "bad_transition"

    rep = (await client.get("/api/ot/reports", params={"from": MON.isoformat(), "to": MON.isoformat()},
                            headers=admin)).json()
    laser_room = next(x for x in rep["rooms"] if x["code"] == "OT-1")
    assert laser_room["available_min"] == 9 * 60  # 10:00–19:00
    assert laser_room["booked_min"] == 30 + 45 and laser_room["booked_utilisation"]["value"] == round(75 / 540 * 100, 1)
    assert laser_room["actual_min"] == 35 + 35 and laser_room["completed"] == 2
    assert laser_room["turnover"] == {"count": 1, "avg_min": 15.0, "median_min": 15.0}
    assert laser_room["on_time_start"]["den"] == 2
    empty = next(x for x in rep["rooms"] if x["code"] == "OT-2")
    assert empty["actual_utilisation"]["value"] == 0.0 and empty["on_time_start"]["value"] is None  # "—", not 0 %
    assert rep["by_equipment"][0]["bookings"] == 2 and rep["by_doctor"][0]["bookings"] == 2
    csv = await client.get("/api/ot/reports.csv", params={"from": MON.isoformat(), "to": MON.isoformat()}, headers=admin)
    assert csv.status_code == 200 and csv.text.startswith("room,available_min") and "All rooms" in csv.text


async def test_setup_screens_are_for_admins(client, doctor, admin, operator):
    assert (await client.get("/api/ot/equipment", headers=operator)).status_code == 200
    r = await client.post("/api/ot/equipment", json={"code": "EQ-9", "name": "Second LED panel",
                                                     "equipment_type": "led_panel"}, headers=doctor)
    assert r.status_code == 403
    r = await client.post("/api/ot/equipment", json={"code": "EQ-9", "name": "Second LED panel",
                                                     "equipment_type": "led_panel"}, headers=admin)
    assert r.status_code == 201
    eid = r.json()["id"]
    try:
        # With two panels, two LED treatments can run at once in different rooms
        async with sessionmaker()() as db:
            await _book(db, ["svc-led-therapy"], "svc-led-therapy", "OT-1", at(MON, 10))
            b = await _book(db, ["svc-led-therapy"], "svc-led-therapy", "OT-3", at(MON, 10))
            assert str(b.equipment_id) == eid
            await db.rollback()
        docs = (await client.get("/api/ot/doctors", headers=doctor)).json()
        demo = next(d for d in docs if d["full_name"] == "Dr. Kavya Rao")
        assert demo["hours"] and (await client.put(f"/api/ot/doctors/{demo['id']}/hours", json={"hours": []},
                                                   headers=doctor)).status_code == 403
        assert (await client.post("/api/ot/holidays", json={"day": TUE.isoformat(), "name": "x"}, headers=doctor)).status_code == 403
    finally:
        async with sessionmaker()() as db:
            await db.delete(await db.get(OtEquipment, uuid.UUID(eid)))
            await db.commit()
