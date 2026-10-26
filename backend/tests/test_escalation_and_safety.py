"""Doctor hand-over rules (spec §05) and safety checks — enforced in code."""
from sqlalchemy import select

from app.db import sessionmaker
from app.integrations.analysis_engine import EngineResult, MockAnalysisEngine, set_analysis_engine
from app.models import AIAction, Escalation
from tests.conftest import Session


async def _to_assess(client, operator, concern="I have pigmentation and dull skin") -> Session:
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say(concern)
    return s


async def test_pregnancy_hands_over_and_blocks_ai(client, operator, doctor):
    s = await _to_assess(client, operator)
    await s.answer_all({"q15": "Pregnant or trying"})
    assert s.ui["mode"] == "handed_over"
    assert s.ui["stage"] == "handed_over"
    assert "doctor" in s.last().lower()

    # The AI stays silent while the doctor has the case
    reply = await s.say("hello?")
    assert reply["messages"] == []

    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    esc = next(i for i in queue["items"] if i["type"] == "escalation")
    assert esc["trigger"] == "pregnancy"

    # Doctor takes over and chats in the same conversation
    r = await client.post(f"/api/doctor/escalations/{esc['id']}/take-over", headers=doctor)
    assert r.status_code == 200
    r = await client.post(f"/api/doctor/conversations/{s.id}/messages", json={"text": "Hi, I'm Dr Rao. Let's keep it gentle."},
                          headers=doctor)
    assert r.json()["role"] == "doctor"

    # Doctor prescribes directly → approved → AI wraps up
    r = await client.post(f"/api/doctor/visits/{esc['visit_id']}/prescribe",
                          json={"items": [{"catalogue_id": "svc-hydrafacial", "sessions": 3}, {"catalogue_id": "prd-sunscreen"}]},
                          headers=doctor)
    assert r.status_code == 200, r.text
    conv = (await client.get(f"/api/conversations/{s.id}", headers=operator)).json()
    assert conv["ui"]["stage"] == "booking"  # HydraFacial needs a room: the AI books it with the client
    await s.book_all()
    assert s.ui["stage"] == "done"


async def test_under_18_escalates_at_identification(client, operator):
    s = Session(client, operator)
    await s.start()
    await s.identify("99000 11122")  # D365 sample contact born 2010
    assert s.ui["mode"] == "handed_over"
    async with sessionmaker()() as db:
        esc = (await db.execute(select(Escalation))).scalars().all()
        assert [e.trigger for e in esc] == ["under_18"]
        assert esc[0].visit_id is None  # before any health data or photos are collected


async def test_user_asks_for_a_person(client, operator):
    s = await _to_assess(client, operator)
    await s.say("Can I talk to a doctor please?")
    assert s.ui["mode"] == "handed_over"
    assert "asked to speak to a person" in s.last()


async def test_medication_conflict_escalates_and_excludes_items(client, operator):
    s = await _to_assess(client, operator, "acne and oily skin")
    await s.answer_all({"q13": "I'm on isotretinoin 20mg"})
    assert s.ui["mode"] == "handed_over"
    async with sessionmaker()() as db:
        esc = (await db.execute(select(Escalation))).scalars().all()
        assert "medication_conflict" in {e.trigger for e in esc}
        acts = (await db.execute(select(AIAction).where(AIAction.action == "escalate_to_doctor"))).scalars().all()
        assert acts and acts[0].actor == "automation" and acts[0].reason.startswith("Rule:")


async def test_low_confidence_escalates(client, operator):
    class LowConf(MockAnalysisEngine):
        async def analyse(self, images, age):
            r = await super().analyse(images, age)
            return EngineResult(r.engine, r.diagnosis, r.anti_aging, 0.42, [])

    set_analysis_engine(LowConf())
    s = await _to_assess(client, operator)
    await s.answer_all()
    await s.photos()
    assert s.ui["mode"] == "handed_over"
    assert "not confident" in s.last()


async def test_non_skincare_finding_escalates(client, operator):
    class Mole(MockAnalysisEngine):
        async def analyse(self, images, age):
            r = await super().analyse(images, age)
            return EngineResult(r.engine, r.diagnosis, r.anti_aging, r.confidence,
                                [{"type": "non_skincare", "note": "Irregular pigmented lesion on left cheek."}])

    set_analysis_engine(Mole())
    s = await _to_assess(client, operator)
    await s.answer_all()
    await s.photos()
    assert s.ui["mode"] == "handed_over"


async def test_doctor_reject_then_prescribe(client, operator, doctor):
    s = await _to_assess(client, operator)
    await s.answer_all()
    await s.photos()
    visit_id = s.ui["visit_id"]
    r = await client.post(f"/api/visits/{visit_id}/approval/decision", json={"decision": "reject", "notes": "Discuss first"},
                          headers=doctor)
    assert r.status_code == 200
    conv = (await client.get(f"/api/conversations/{s.id}", headers=operator)).json()
    assert conv["ui"]["mode"] == "handed_over"
    # The AI stays out of it: a client message gets no AI reply and no plan is redrafted.
    assert (await s.say("ok, what now?"))["messages"] == []
    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    esc = next(i for i in queue["items"] if i.get("trigger") == "doctor_rejected")
    # Handing back would just resubmit the rejected plan, so it's refused.
    r = await client.post(f"/api/doctor/escalations/{esc['id']}/resolve", json={"hand_back_to_ai": True}, headers=doctor)
    assert r.status_code == 409
    r = await client.post(f"/api/doctor/visits/{visit_id}/prescribe",
                          json={"items": [{"catalogue_id": "svc-barrier-facial", "sessions": 2}, {"catalogue_id": "prd-sunscreen"}]},
                          headers=doctor)
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/api/visits/{visit_id}", headers=doctor)).json()
    assert detail["prescription"]["version"] == 2 and detail["prescription"]["status"] == "approved"
    # The client would rather book the facial later: the front desk gets it on their list.
    await s.refresh()
    assert s.ui["stage"] == "booking"
    await s.say("Book later")
    assert "front desk will call you" in s.transcript[-2]["content"]
    detail = (await client.get(f"/api/visits/{visit_id}", headers=doctor)).json()
    assert detail["visit"]["status"] == "completed"
    assert [b["status"] for b in detail["ot_bookings"]] == ["deferred"]
    todo = (await client.get("/api/ot/schedule", headers=doctor)).json()["to_schedule"]
    assert [t["item_id"] for t in todo if t["visit_id"] == visit_id] == ["svc-barrier-facial"]
    assert not [e for e in detail["escalations"] if e["status"] != "resolved"]


async def test_close_without_ai_then_prescribe_still_wraps_up(client, operator, doctor):
    s = await _to_assess(client, operator)
    await s.answer_all()
    await s.photos()
    await s.say("Can I speak to someone please")
    visit_id = s.ui["visit_id"]
    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    esc = next(i for i in queue["items"] if i["type"] == "escalation")
    r = await client.post(f"/api/doctor/escalations/{esc['id']}/resolve", json={"hand_back_to_ai": False}, headers=doctor)
    assert r.status_code == 200
    # Still visible to doctors (pending approval), not lost
    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    assert any(i["visit_id"] == visit_id for i in queue["items"])
    r = await client.post(f"/api/visits/{visit_id}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert r.status_code == 200
    await s.book_all()
    detail = (await client.get(f"/api/visits/{visit_id}", headers=doctor)).json()
    assert detail["visit"]["status"] == "completed" and detail["visit"]["next_session_date"]
    assert {"add_note", "share_summary", "set_next_session"} <= {a["action"] for a in detail["actions"] if a["status"] == "ok"}


async def test_hand_back_only_affects_its_own_conversation(client, operator, doctor):
    a, b = Session(client, operator), Session(client, operator)
    for sess, mobile in ((a, "98765 43210"), (b, "98111 22233")):
        await sess.start()
        await sess.identify(mobile)
        await sess.say("I want to talk to a real person")
        assert sess.ui["mode"] == "handed_over"
    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    esc_a = next(i for i in queue["items"] if i.get("conversation_id") == a.id)
    r = await client.post(f"/api/doctor/escalations/{esc_a['id']}/resolve", json={"hand_back_to_ai": True}, headers=doctor)
    assert r.status_code == 200
    ui_a = (await client.get(f"/api/conversations/{a.id}", headers=operator)).json()["ui"]
    ui_b = (await client.get(f"/api/conversations/{b.id}", headers=operator)).json()["ui"]
    assert ui_a["mode"] == "ai" and ui_a["stage"] == "concern"
    assert ui_b["mode"] == "handed_over"


async def test_staff_rest_escalation_stops_the_ai(client, operator, doctor):
    s = await _to_assess(client, operator)
    visit_id = s.ui["visit_id"]
    r = await client.put(f"/api/visits/{visit_id}/assessment", json={"answers": {"q15": "pregnant"}}, headers=operator)
    assert r.status_code == 200
    ui = (await client.get(f"/api/conversations/{s.id}", headers=operator)).json()["ui"]
    assert ui["stage"] == "handed_over" and ui["mode"] == "handed_over"
    assert (await s.say("hello"))["messages"] == []


async def test_unknown_date_of_birth_is_collected(client, operator):
    r = await client.post("/api/clients", json={"mobile": "9000000001", "full_name": "Kiran Das"}, headers=operator)
    assert r.status_code == 201 and r.json()["dob"] is None
    s = Session(client, operator)
    await s.start()
    await s.identify("9000000001")
    assert "date of birth" in s.last()
    assert s.ui["stage"] == "identify" and s.ui["input"] == "date"
    await s.say("02-03-2011")  # under 18 → hand-over before any health data is collected
    assert s.ui["mode"] == "handed_over"


async def test_catalogue_only_and_role_guards(client, operator, doctor):
    s = await _to_assess(client, operator)
    await s.answer_all()
    await s.photos()
    visit_id = s.ui["visit_id"]
    # Operators cannot approve
    r = await client.post(f"/api/visits/{visit_id}/approval/decision", json={"decision": "approve"}, headers=operator)
    assert r.status_code == 403
    # Doctors can edit, but only with catalogue items
    r = await client.post(f"/api/visits/{visit_id}/approval/decision",
                          json={"decision": "edit", "items": [{"catalogue_id": "made-up-miracle-cream"}]}, headers=doctor)
    assert r.status_code == 422
    assert "not in the clinic catalogue" in r.json()["detail"]
    r = await client.post(f"/api/visits/{visit_id}/approval/decision",
                          json={"decision": "edit", "items": [{"catalogue_id": "svc-glycolic-peel", "sessions": 4},
                                                              {"catalogue_id": "prd-sunscreen"}]},
                          headers={**doctor, "Idempotency-Key": "decide-once-1"})
    assert r.status_code == 200
    # Exactly once: the same key replays the first response; a new key is refused by the state machine
    again = await client.post(f"/api/visits/{visit_id}/approval/decision",
                              json={"decision": "edit", "items": [{"catalogue_id": "prd-sunscreen"}]},
                              headers={**doctor, "Idempotency-Key": "decide-once-1"})
    assert again.status_code == 200 and again.json()["approval"]["status"] == "edited"
    third = await client.post(f"/api/visits/{visit_id}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert third.status_code == 409
    summary = (await client.get(f"/api/visits/{visit_id}/summary", headers=operator)).json()
    assert [p["name"] for p in summary["plan"]] == ["Glycolic Peel", "Sunscreen SPF 50 PA++++"]


async def test_ai_cannot_skip_stages(client, operator):
    """Design decision D2: tools outside the current stage are blocked and logged."""
    from app.ai.tools import ActionLayer

    s = Session(client, operator)
    await s.start()
    async with sessionmaker()() as db:
        layer = ActionLayer(db, s.id)
        r = await layer.call("draft_prescription", {"items": [{"catalogue_id": "prd-sunscreen"}]}, "trying to skip ahead")
        assert r["status"] == "blocked"
        logged = (await db.execute(select(AIAction).where(AIAction.action == "draft_prescription"))).scalar_one()
        assert logged.status == "blocked" and logged.stage == "identify"
