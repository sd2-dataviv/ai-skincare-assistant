"""OT / procedure-room availability and booking (FR-19).

Service tests use a fixed 'now' so slot times are exact; flow tests drive the chat and staff API like the app does.
"""
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.ai import agents
from app.ai.agents import parse_day
from app.db import sessionmaker
from app.models import CatalogueItem, Client, OtBooking, OtRoom, Prescription, Visit
from app.services import DomainError
from app.services import ot
from tests.conftest import Session

IST = ZoneInfo("Asia/Kolkata")
SAT_EVENING = datetime(2026, 10, 24, 18, 0, tzinfo=IST)  # Saturday; rooms close at 19:00, closed Sunday
MON = date(2026, 10, 26)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), IST)


async def _visit_with_plan(db, items: list[str]) -> Visit:
    import uuid

    c = Client(mobile="+91" + str(uuid.uuid4().int)[:10], full_name="Test Client")
    db.add(c)
    await db.flush()
    v = Visit(client_id=c.id, programme="acne", status="approved", concerns=["acne"])
    db.add(v)
    await db.flush()
    rows = {r.id: r for r in (await db.execute(select(CatalogueItem).where(CatalogueItem.id.in_(items)))).scalars()}
    db.add(Prescription(visit_id=v.id, status="approved", items=[
        {"catalogue_id": i, "kind": rows[i].kind, "name": rows[i].name, "sessions": rows[i].default_sessions} for i in items]))
    await db.flush()
    return v


# ── Availability rules ───────────────────────────────────────────────────────
async def test_slots_only_in_rooms_that_suit_the_procedure():
    async with sessionmaker()() as db:
        laser = await db.get(CatalogueItem, "svc-qswitch-toning")
        facial = await db.get(CatalogueItem, "svc-hydrafacial")
        peel = await db.get(CatalogueItem, "svc-glycolic-peel")
        assert {s["room_code"] for s in await ot.find_slots(db, laser, count=6, now=SAT_EVENING)} == {"OT-1"}
        assert {s["room_code"] for s in await ot.find_slots(db, facial, count=6, now=SAT_EVENING)} == {"OT-3"}
        assert {s["room_code"] for s in await ot.find_slots(db, peel, count=6, now=SAT_EVENING)} <= {"OT-2", "OT-3"}
        with pytest.raises(DomainError):
            await ot.find_slots(db, await db.get(CatalogueItem, "prd-sunscreen"), now=SAT_EVENING)


async def test_opening_hours_lead_time_and_spread():
    async with sessionmaker()() as db:
        led = await db.get(CatalogueItem, "svc-led-therapy")
        slots = await ot.find_slots(db, led, now=SAT_EVENING)
        # Saturday is too late (2 h lead, closes 19:00), Sunday is closed: Monday, one per part of the day.
        assert [s["label"] for s in slots] == ["Mon 26 Oct, 10:00 am", "Mon 26 Oct, 12:00 pm", "Mon 26 Oct, 4:00 pm"]
        # A procedure must finish by closing time: a 60-minute facial can't start at 18:30.
        facial = await db.get(CatalogueItem, "svc-hydrafacial")
        evening = await ot.find_slots(db, facial, day=MON, part="evening", count=12, now=SAT_EVENING)
        assert evening[-1]["label"].endswith("6:00 pm") and all(not s["label"].endswith("6:30 pm") for s in evening)


async def test_turnover_buffer_and_blocks_are_respected():
    async with sessionmaker()() as db:
        laser = await db.get(CatalogueItem, "svc-qswitch-toning")
        v = await _visit_with_plan(db, ["svc-qswitch-toning", "svc-carbon-laser"])
        room = (await db.execute(select(OtRoom).where(OtRoom.code == "OT-1"))).scalar_one()
        await ot.book(db, visit=v, item=laser, room_id=room.id, starts_at=at(MON, 10), user_id=None, source="staff",
                      now=SAT_EVENING)
        # 10:00–10:30 plus 15 min turnover: the room is next free at 10:45, so the next grid slot is 11:00.
        nxt = await ot.find_slots(db, await db.get(CatalogueItem, "svc-carbon-laser"), day=MON, part="morning", now=SAT_EVENING)
        assert nxt[0]["label"] == "Mon 26 Oct, 11:00 am"
        # A maintenance block on Monday pushes laser work to Tuesday.
        await ot.add_block(db, room_id=room.id, starts_at=at(MON, 11), ends_at=at(MON, 19), reason="Laser service",
                           user_id=None)
        first = (await ot.find_slots(db, await db.get(CatalogueItem, "svc-carbon-laser"), now=SAT_EVENING))[0]
        assert first["label"] == "Tue 27 Oct, 10:00 am"
        # And a block can't be laid over a confirmed booking.
        with pytest.raises(DomainError) as e:
            await ot.add_block(db, room_id=room.id, starts_at=at(MON, 9), ends_at=at(MON, 10, 30), reason="x", user_id=None)
        assert e.value.code == "conflicts_with_bookings"
        await db.rollback()


async def test_no_double_booking_in_code_or_in_the_database():
    async with sessionmaker()() as db:
        laser = await db.get(CatalogueItem, "svc-qswitch-toning")
        carbon = await db.get(CatalogueItem, "svc-carbon-laser")
        v1 = await _visit_with_plan(db, ["svc-qswitch-toning"])
        v2 = await _visit_with_plan(db, ["svc-carbon-laser"])
        room = (await db.execute(select(OtRoom).where(OtRoom.code == "OT-1"))).scalar_one()
        b = await ot.book(db, visit=v1, item=laser, room_id=room.id, starts_at=at(MON, 10), user_id=None, source="ai",
                          now=SAT_EVENING)
        with pytest.raises(DomainError) as e:  # overlaps the turnover of the first booking
            await ot.book(db, visit=v2, item=carbon, room_id=room.id, starts_at=at(MON, 10, 30), user_id=None,
                          source="ai", now=SAT_EVENING)
        assert e.value.code == "slot_taken"
        with pytest.raises(DomainError) as e:
            await ot.book(db, visit=v1, item=laser, room_id=room.id, starts_at=at(MON, 15), user_id=None, source="ai",
                          now=SAT_EVENING)
        assert e.value.code == "already_booked"
        with pytest.raises(DomainError) as e:  # not a laser room
            facial_room = (await db.execute(select(OtRoom).where(OtRoom.code == "OT-3"))).scalar_one()
            await ot.book(db, visit=v2, item=carbon, room_id=facial_room.id, starts_at=at(MON, 12), user_id=None,
                          source="ai", now=SAT_EVENING)
        assert e.value.code == "room_unsuitable"
        await db.commit()
        # Even writing straight to the table can't create an overlap: the exclusion constraint refuses it.
        db.add(OtBooking(visit_id=v2.id, client_id=v2.client_id, catalogue_item_id=carbon.id, status="confirmed",
                         room_id=room.id, starts_at=b.starts_at + timedelta(minutes=5), ends_at=b.ends_at,
                         clear_until=b.clear_until, source="staff"))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


@pytest.mark.parametrize("phrase,expected", [
    ("tomorrow morning", (date(2026, 10, 18), "morning")),
    ("saturday", (date(2026, 10, 17), None)),  # today is Saturday
    ("monday", (date(2026, 10, 19), None)),
    ("next saturday afternoon", (date(2026, 10, 24), "afternoon")),
    ("how about 21 oct", (date(2026, 10, 21), None)),
    ("Oct 30th in the evening", (date(2026, 10, 30), "evening")),
    ("22/10", (date(2026, 10, 22), None)),
    ("after lunch", (None, "afternoon")),
    ("I'm not sure", (None, None)),
])
def test_parse_day(phrase, expected):
    assert parse_day(phrase, date(2026, 10, 17)) == expected  # a Saturday


# ── In the chat ──────────────────────────────────────────────────────────────
async def _to_booking(client, operator, doctor, items: list[dict] | None = None) -> Session:
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()
    body = {"decision": "approve"} if items is None else {"decision": "edit", "items": items}
    r = await client.post(f"/api/visits/{s.ui['visit_id']}/approval/decision", json=body, headers=doctor)
    assert r.status_code == 200, r.text
    await s.refresh()
    s.transcript += r.json()["conversation_messages"]
    assert s.ui["stage"] == "booking", s.ui
    return s


async def test_chat_later_dates_a_named_day_and_picking_by_position(client, operator, doctor):
    s = await _to_booking(client, operator, doctor, [{"catalogue_id": "svc-led-therapy", "sessions": 6},
                                                     {"catalogue_id": "prd-sunscreen"}])
    first = s.ui["booking"]["slots"]
    assert s.ui["booking"]["procedure"] == "LED Light Therapy"
    await s.say("Later dates")
    later = s.ui["booking"]["slots"]
    assert later[0]["starts_at"] > first[-1]["starts_at"]
    # A day that suits the client: three days ahead (skipping Sunday), in the morning.
    d = date.today() + timedelta(days=3)
    if d.weekday() == 6:
        d += timedelta(days=1)
    await s.say(f"do you have anything on {d.day} {d:%b} in the morning?")
    asked = s.ui["booking"]["slots"]
    assert asked and all(datetime.fromisoformat(x["starts_at"]).astimezone(IST).date() == d for x in asked)
    assert all(datetime.fromisoformat(x["starts_at"]).astimezone(IST).hour < 12 for x in asked)
    await s.say("how long does it take?")  # scripted mode can't answer, but says so kindly and keeps the offer
    assert s.transcript[-2]["content"].startswith("Good question.") and s.ui["booking"]["slots"] == asked
    await s.say("the second one")
    assert s.transcript[-2]["content"].startswith(
        f"I've held {asked[1]['label'].replace(', ', ' at ')} in the {asked[1]['room_name']} for your LED Light Therapy.")
    assert s.ui["stage"] == "done" and "Your times are held" in s.last()


async def test_two_treatments_are_booked_one_after_the_other(client, operator, doctor):
    s = await _to_booking(client, operator, doctor, [{"catalogue_id": "svc-salicylic-peel", "sessions": 6},
                                                     {"catalogue_id": "svc-led-therapy", "sessions": 6},
                                                     {"catalogue_id": "prd-sunscreen"}])
    assert s.ui["booking"]["procedure"] == "Salicylic Acid Peel" and s.ui["booking"]["remaining"] == 2
    await s.say(s.ui["quick_replies"][0])
    assert s.ui["stage"] == "booking" and s.ui["booking"]["procedure"] == "LED Light Therapy"
    assert "Let's book your LED Light Therapy" in s.last()
    await s.say(s.ui["quick_replies"][0])
    assert s.ui["stage"] == "done"
    assert "Salicylic Acid Peel on" in s.last() and "LED Light Therapy on" in s.last()


async def test_slot_taken_between_offer_and_tap(client, operator, doctor):
    s = await _to_booking(client, operator, doctor, [{"catalogue_id": "svc-qswitch-toning", "sessions": 6}])
    slot = s.ui["booking"]["slots"][0]
    # Another client takes that exact slot first. Same treatment, so it fits the slot at any time of day (a longer one
    # could run past closing when the test runs late in the afternoon, and would then need an override).
    other = await _visit_quick(["svc-qswitch-toning"])
    r = await client.post("/api/ot/bookings", json={"visit_id": str(other), "item_id": "svc-qswitch-toning",
                                                     "room_id": slot["room_id"], "starts_at": slot["starts_at"]},
                          headers=doctor)
    assert r.status_code == 201, r.text
    await s.say(slot["label"])
    texts = [m["content"] for m in s.transcript[-3:]]
    assert "Sorry, that time has just been taken." in texts
    assert s.ui["stage"] == "booking" and slot["starts_at"] not in [x["starts_at"] for x in s.ui["booking"]["slots"]]


async def _visit_quick(items: list[str]):
    async with sessionmaker()() as db:
        v = await _visit_with_plan(db, items)
        v.status = "completed"
        await db.commit()
        return v.id


# ── Staff calendar and endpoints ─────────────────────────────────────────────
async def test_staff_calendar_blocks_and_booking_a_deferred_treatment(client, operator, doctor, admin):
    s = await _to_booking(client, operator, doctor, [{"catalogue_id": "svc-hydrafacial", "sessions": 4}])
    await s.say("I'll call later to book, thanks")
    assert s.ui["stage"] == "done"
    vid = s.ui["visit_id"]
    # The clinic device faces the client: no calendar (other clients' names), no booking outside the chat.
    assert (await client.get("/api/ot/schedule", headers=operator)).status_code == 403
    assert (await client.get("/api/ot/availability", params={"item_id": "svc-hydrafacial"}, headers=operator)).status_code == 403
    sched = (await client.get("/api/ot/schedule", headers=admin)).json()
    assert [r["code"] for r in sched["rooms"]] == ["OT-1", "OT-2", "OT-3"]
    todo = [t for t in sched["to_schedule"] if t["visit_id"] == vid]
    assert todo and todo[0]["deferred"] and todo[0]["procedure"] == "HydraFacial"

    avail = (await client.get("/api/ot/availability", params={"item_id": "svc-hydrafacial"}, headers=admin)).json()
    slot = avail["slots"][0]
    r = await client.post("/api/ot/bookings", json={"visit_id": vid, "item_id": "svc-hydrafacial", "room_id": slot["room_id"],
                                                     "starts_at": slot["starts_at"]}, headers=admin)
    assert r.status_code == 201, r.text
    booking = r.json()
    day = datetime.fromisoformat(slot["starts_at"]).astimezone(IST).date().isoformat()
    sched = (await client.get("/api/ot/schedule", params={"day": day}, headers=admin)).json()
    room = next(x for x in sched["rooms"] if x["id"] == slot["room_id"])
    assert [b["id"] for b in room["bookings"]] == [booking["id"]] and room["bookings"][0]["client_name"]
    assert not [t for t in sched["to_schedule"] if t["visit_id"] == vid]

    # Blocks: doctors and admins only, and never on top of a booking
    blk = {"room_id": slot["room_id"], "starts_at": slot["starts_at"], "ends_at": booking["ends_at"], "reason": "Deep clean"}
    assert (await client.post("/api/ot/blocks", json=blk, headers=operator)).status_code == 403
    assert (await client.post("/api/ot/blocks", json=blk, headers=doctor)).status_code == 409
    # Cancel → back on the to-schedule list; now the block can go in
    assert (await client.post(f"/api/ot/bookings/{booking['id']}/cancel", json={}, headers=operator)).status_code == 403
    r = await client.post(f"/api/ot/bookings/{booking['id']}/cancel", json={"reason": "Client unwell"}, headers=admin)
    assert r.json()["status"] == "cancelled"
    assert (await client.post("/api/ot/blocks", json=blk, headers=doctor)).status_code == 201
    todo = (await client.get("/api/ot/schedule", headers=admin)).json()["to_schedule"]
    assert [t["item_id"] for t in todo if t["visit_id"] == vid] == ["svc-hydrafacial"]

    # Rooms are managed by admins
    new_room = {"code": "OT-4", "name": "Minor OT", "capabilities": ["minor_procedure"], "open_time": "09:00",
                "close_time": "17:00"}
    assert (await client.post("/api/ot/rooms", json=new_room, headers=doctor)).status_code == 403
    r = await client.post("/api/ot/rooms", json=new_room, headers=admin)
    assert r.status_code == 201
    r = await client.patch(f"/api/ot/rooms/{r.json()['id']}", json={"active": False}, headers=admin)
    assert r.json()["active"] is False
    async with sessionmaker()() as db:
        await db.execute(text("DELETE FROM ot_rooms WHERE code = 'OT-4'"))
        await db.commit()


async def test_staff_booking_moves_a_waiting_chat_on(client, operator, doctor, admin):
    s = await _to_booking(client, operator, doctor, [{"catalogue_id": "svc-hydrafacial", "sessions": 4}])
    slot = s.ui["booking"]["slots"][2]
    r = await client.post("/api/ot/bookings", json={"visit_id": s.ui["visit_id"], "item_id": "svc-hydrafacial",
                                                     "room_id": slot["room_id"], "starts_at": slot["starts_at"]},
                          headers=admin)
    assert r.status_code == 201, r.text
    assert "You're all set" in r.json()["messages"][-1]["content"]
    await s.refresh()
    assert s.ui["stage"] == "done"


async def test_plan_without_treatments_skips_booking(client, operator, doctor):
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()
    r = await client.post(f"/api/visits/{s.ui['visit_id']}/approval/decision",
                          json={"decision": "edit", "items": [{"catalogue_id": "prd-sunscreen"}]}, headers=doctor)
    assert "sent the summary" in r.json()["conversation_messages"][0]["content"]
    await s.refresh()
    assert s.ui["stage"] == "done"


# ── Conversational layer ─────────────────────────────────────────────────────
class FakeProvider:
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def chat(self, messages, tools=None, temperature=0.3):
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("LLM called but nothing scripted")
        return self.script.pop(0)


def decide(intent, reply="", value=""):
    return {"role": "assistant", "content": json.dumps({"intent": intent, "value": value, "reply": reply})}


async def test_llm_free_text_in_booking(client, operator, doctor, monkeypatch):
    s = await _to_booking(client, operator, doctor, [{"catalogue_id": "svc-led-therapy", "sessions": 6}])
    d = date.today() + timedelta(days=4)
    if d.weekday() == 6:
        d += timedelta(days=1)
    fake = FakeProvider([
        decide("other", "Yes, it takes about 30 minutes and you'll be booked in for Monday."),  # wrong claim: dropped
        decide("date", "", f"{d.isoformat()} afternoon"),
        decide("slot", "Lovely!", ""),
    ])
    monkeypatch.setattr(agents, "get_provider", lambda: fake)
    out = await s.say("how long does it take?")
    assert all("booked" not in m["content"] for m in out["messages"])
    assert out["messages"][-1]["content"].startswith("When would you like your LED Light Therapy?")
    system = fake.calls[0][0]["content"]
    assert "OFFERED_SLOTS" in system and "TODAY" in system
    await s.say("anything that afternoon?")
    asked = s.ui["booking"]["slots"]
    assert all(datetime.fromisoformat(x["starts_at"]).astimezone(IST).date() == d for x in asked)
    assert all(12 <= datetime.fromisoformat(x["starts_at"]).astimezone(IST).hour < 16 for x in asked)
    n = len(fake.calls)
    await s.say(asked[0]["label"])  # a tap: no model call
    assert len(fake.calls) == n
    assert s.ui["stage"] == "done"
