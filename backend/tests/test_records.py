"""Records: every consultation stays findable by doctors and admins after it leaves the review queue."""
from tests.conftest import Session


async def _finished_visit(client, operator, doctor) -> Session:
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()
    r = await client.post(f"/api/visits/{s.ui['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert r.status_code == 200
    await s.book_all()
    assert s.ui["stage"] == "done"
    return s


async def test_completed_visit_stays_on_record_with_transcript_report_and_history(client, operator, doctor, admin):
    s = await _finished_visit(client, operator, doctor)
    vid = s.ui["visit_id"]
    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    assert not [i for i in queue["items"] if i["visit_id"] == vid]  # gone from the queue…

    for who in (doctor, admin):  # …but on record for doctors and admins
        rec = (await client.get("/api/doctor/records", headers=who)).json()
        row = next(i for i in rec["items"] if i["visit_id"] == vid)
        assert row["outcome"] == "completed" and row["client_name"] == "Priya Sharma"
        assert row["decisions"][0]["status"] == "approved" and row["decisions"][0]["doctor"] == "Dr. Kavya Rao"
        assert row["handled_by"] == ["Dr. Kavya Rao"] and row["treatments_booked"] >= 1
        assert row["mobile"].endswith("3210") and "98765" not in row["mobile"]

    detail = (await client.get(f"/api/visits/{vid}", headers=admin)).json()
    assert detail["prescription"]["status"] == "approved" and detail["analyses"] and detail["photos"]
    assert detail["people"][detail["approvals"][-1]["doctor_id"]] == "Dr. Kavya Rao"
    assert detail["notes"] and detail["notes"][0]["author_type"] == "ai"
    chat = (await client.get(f"/api/doctor/conversations/{s.id}", headers=admin)).json()
    assert len(chat["messages"]) > 20 and chat["client"]["full_name"] == "Priya Sharma"

    # Kiosk can't browse other clients' records
    assert (await client.get("/api/doctor/records", headers=operator)).status_code == 403


async def test_rejected_and_pre_visit_handovers_are_on_record_and_filterable(client, operator, doctor):
    # A rejected plan, then the doctor prescribes
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()
    vid = s.ui["visit_id"]
    await client.post(f"/api/visits/{vid}/approval/decision", json={"decision": "reject", "notes": "Discuss first"},
                      headers=doctor)
    await client.post(f"/api/doctor/visits/{vid}/prescribe", json={"items": [{"catalogue_id": "prd-sunscreen"}]},
                      headers=doctor)

    # A 16-year-old: handed over at identification, before any visit
    u18 = Session(client, operator)
    await u18.start()
    await u18.identify("99000 11122")
    assert u18.ui["mode"] == "handed_over"

    # Someone who walked away before giving a number: not a record
    gone = Session(client, operator)
    await gone.start()

    rec = (await client.get("/api/doctor/records", headers=doctor)).json()
    ids = {i["conversation_id"] for i in rec["items"]}
    assert s.id in ids and u18.id in ids and gone.id not in ids

    rejected = (await client.get("/api/doctor/records", params={"outcome": "rejected"}, headers=doctor)).json()["items"]
    assert [i["visit_id"] for i in rejected] == [vid]
    assert [d["status"] for d in rejected[0]["decisions"]] == ["rejected", "approved"]

    pre = (await client.get("/api/doctor/records", params={"outcome": "not_started"}, headers=doctor)).json()["items"]
    row = next(i for i in pre if i["conversation_id"] == u18.id)
    assert row["visit_id"] is None and row["escalations"][0]["trigger"] == "under_18"
    assert row["outcome_label"].startswith("With a doctor")
    # The under-18 hand-over must not leak into the other client's record
    other = next(i for i in rec["items"] if i["conversation_id"] == s.id)
    assert "under_18" not in [e["trigger"] for e in other["escalations"]]

    chat = (await client.get(f"/api/doctor/conversations/{u18.id}", headers=doctor)).json()
    assert chat["escalations"][0]["trigger"] == "under_18" and chat["messages"]

    by_name = (await client.get("/api/doctor/records", params={"q": "ananya"}, headers=doctor)).json()["items"]
    assert [i["conversation_id"] for i in by_name] == [u18.id]
    by_mobile = (await client.get("/api/doctor/records", params={"q": "43210"}, headers=doctor)).json()["items"]
    assert [i["conversation_id"] for i in by_mobile] == [s.id]
