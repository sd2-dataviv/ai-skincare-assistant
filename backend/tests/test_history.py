"""A returning client sees their own past consultations on the clinic device, and nothing of anyone else's."""
from tests.conftest import Session


async def _finished_visit(client, operator, doctor, mobile="98765 43210") -> Session:
    s = Session(client, operator)
    await s.start()
    await s.identify(mobile)
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()
    r = await client.post(f"/api/visits/{s.ui['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert r.status_code == 200
    await s.book_all()
    return s


async def test_returning_client_sees_their_history(client, operator, doctor):
    first = await _finished_visit(client, operator, doctor)
    old_visit = first.ui["visit_id"]

    s = Session(client, operator)
    await s.start()
    assert (await client.get(f"/api/conversations/{s.id}/history", headers=operator)).json() == {"visits": [], "upcoming": []}
    await s.identify()
    assert "Welcome back, Priya! Your last visit was on" in s.last() and "History tab" in s.last()
    assert "Acne Scription, session 1" in s.last()
    assert s.ui["history_count"] == 1

    h = (await client.get(f"/api/conversations/{s.id}/history", headers=operator)).json()
    v = h["visits"][0]
    assert v["visit_id"] == old_visit and v["status"] == "Completed" and v["approved"]
    assert v["plan"] and v["findings"] and v["appointments"][0]["status"] == "tentative"
    assert h["upcoming"] and h["upcoming"][0]["procedure"] == v["appointments"][0]["procedure"]

    r = await client.get(f"/api/conversations/{s.id}/visits/{old_visit}/summary", headers=operator)
    assert r.status_code == 200 and r.json()["plan"]

    # Once today's visit starts, history still lists only the earlier one
    await s.say("Following up on my acne")
    assert s.ui["history_count"] == 1


async def test_history_never_shows_another_clients_visits(client, operator, doctor):
    priya = await _finished_visit(client, operator, doctor)
    rohan = Session(client, operator)
    await rohan.start()
    await rohan.identify("98111 22233")
    assert rohan.ui["history_count"] == 0 and "Rohan" in rohan.last() and "History" not in rohan.last()
    assert (await client.get(f"/api/conversations/{rohan.id}/history", headers=operator)).json()["visits"] == []
    r = await client.get(f"/api/conversations/{rohan.id}/visits/{priya.ui['visit_id']}/summary", headers=operator)
    assert r.status_code == 404


async def test_unfinished_visit_is_listed_without_plan_or_findings(client, operator, doctor):
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()  # draft waits for a doctor: not approved
    vid = s.ui["visit_id"]

    again = Session(client, operator)
    await again.start()
    await again.identify()
    v = (await client.get(f"/api/conversations/{again.id}/history", headers=operator)).json()["visits"][0]
    assert v["visit_id"] == vid and v["status"] == "Not finished" and not v["approved"]
    assert v["plan"] == [] and v["findings"] == []
    r = await client.get(f"/api/conversations/{again.id}/visits/{vid}/summary", headers=operator)
    assert r.status_code == 403  # no summary before a doctor approves
