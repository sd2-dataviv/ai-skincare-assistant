"""The clinic's consultation services: Acne / Pigmentation / Age / Skin Scription, Anti Aging, Medical Consultation,
and their follow-ups (Follow Up, Medical Follow Up) from session 2."""
import uuid

from app.db import sessionmaker
from app.models import Client, Visit
from app.services.recommendations import recommend
from app.services.visits import programme_for, programme_label, services, start_visit


def test_the_concern_picks_the_service():
    def pick(text: str) -> str | None:
        from app.services.visits import detect_concerns

        return programme_for(detect_concerns(text), text)

    assert pick("pimples on my cheeks") == "acne"
    assert pick("dark spots and melasma") == "pigmentation"
    assert pick("fine lines around my eyes") == "age"
    assert pick("my skin looks dull and dry") == "skin"
    assert pick("I'd like Botox for my forehead lines") == "anti_aging"  # doctor-led, wins over the wrinkles
    assert pick("an itchy rash on my arm") == "medical"
    assert pick("eczema patches and some acne") == "medical"  # a medical condition wins over skin concerns
    assert pick("hello") is None
    assert pick("I switched my cleanser and now I have pimples") == "acne"  # "switched" is not "itch"


def test_service_names_and_follow_ups():
    assert [s["label"] for s in services()] == ["Acne Scription", "Pigmentation Scription", "Age Scription",
                                                  "Skin Scription", "Anti Aging", "Medical Consultation"]
    assert programme_label("acne", 1) == "Acne Scription"
    assert programme_label("acne", 2) == "Follow Up (Acne Scription)"
    assert programme_label("medical", 1) == "Medical Consultation"
    assert programme_label("medical", 3) == "Medical Follow Up"
    assert programme_label("anti_aging", 2) == "Follow Up (Anti Aging)"
    # Visits filed before the rename keep working.
    assert programme_label("anti_ageing", 1) == "Age Scription"
    assert programme_label("glow_hydration", 1) == programme_label("general", 1) == "Skin Scription"


async def _client(db) -> Client:
    c = Client(mobile="+91" + str(uuid.uuid4().int)[:10], full_name="Test Client")
    db.add(c)
    await db.flush()
    return c


async def test_a_returning_client_gets_the_follow_up_of_their_service():
    async with sessionmaker()() as db:
        c = await _client(db)
        v1 = await start_visit(db, c, "an itchy rash on my neck", None)
        assert (v1.programme, v1.session_number) == ("medical", 1)
        v2 = await start_visit(db, c, "checking how it's healing", None)  # nothing new: the same service continues
        assert (v2.programme, v2.session_number) == ("medical", 2)
        assert programme_label(v2.programme, v2.session_number) == "Medical Follow Up"
        v3 = await start_visit(db, c, "now I have pimples too", None)  # a new concern: a new service
        assert (v3.programme, v3.session_number) == ("acne", 1)
        v4 = await start_visit(db, c, "follow up on my acne", None)
        assert programme_label(v4.programme, v4.session_number) == "Follow Up (Acne Scription)"
        # A visit filed under an old key is still counted towards its service.
        db.add(Visit(client_id=c.id, programme="glow_hydration", session_number=1, concerns=["dullness"]))
        await db.flush()
        v5 = await start_visit(db, c, "my skin looks dull", None)
        assert (v5.programme, v5.session_number) == ("skin", 2)
        await db.rollback()


async def test_anti_aging_includes_the_doctors_consultation():
    async with sessionmaker()() as db:
        c = await _client(db)
        v = await start_visit(db, c, "I want Botox", None)
        assert v.programme == "anti_aging" and v.concerns == ["wrinkles"]
        plan = await recommend(db, v, c, None, {"wrinkles": {"score": 30}})  # mild lines: no severity trigger
        names = [i["name"] for i in plan["items"]]
        assert "Anti-ageing Injectables Consultation (Doctor)" in names
        await db.rollback()


async def test_services_endpoint(client, operator):
    r = await client.get("/api/knowledge/services", headers=operator)
    assert r.status_code == 200
    by_key = {s["key"]: s for s in r.json()}
    assert by_key["medical"]["follow_up"] == "Medical Follow Up" and by_key["medical"]["doctor_led"]
    assert by_key["skin"]["follow_up"] == "Follow Up (Skin Scription)"


async def test_a_medical_problem_handed_to_a_doctor_is_filed_as_a_medical_consultation(client, operator):
    from sqlalchemy import select

    from app.models import Escalation
    from tests.conftest import Session

    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I burnt my cheek with hot oil this morning")
    assert s.ui["mode"] == "handed_over"
    assert s.ui["programme"] == "Medical Consultation" and s.ui["session_number"] == 1
    said = " ".join(m["content"] for m in s.transcript[-3:])
    assert "Medical Consultation" in said and "needs a doctor" in s.last()  # the hand-over message stays last
    async with sessionmaker()() as db:
        esc = (await db.execute(select(Escalation))).scalars().all()
        assert [e.trigger for e in esc] == ["medical_concern"] and esc[0].visit_id is not None

    # The same client back with the same problem: a Medical Follow Up.
    again = Session(client, operator)
    await again.start()
    await again.identify()
    await again.say("the burn on my cheek is blistering now")
    assert again.ui["programme"] == "Medical Follow Up" and again.ui["session_number"] == 2


async def test_the_llm_handing_over_a_medical_problem_files_it_too(client, operator, monkeypatch):
    from app.ai import agents
    from tests.test_llm_agent import FakeProvider, decide, _start

    fake = FakeProvider([decide("medical", "I'm sorry, that sounds uncomfortable.")])
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify()
    await s.say("I have eczema patches on my arms that keep flaring")
    assert s.ui["mode"] == "handed_over" and s.ui["programme"] == "Medical Consultation"
    assert agents  # the fake provider was used
