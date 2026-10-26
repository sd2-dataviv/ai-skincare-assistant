"""End-to-end consultation through the API (spec §03 steps 1–8)."""
from sqlalchemy import select

from app.db import sessionmaker
from app.integrations.messaging import OUTBOX
from app.models import AIAction, Note, Visit
from tests.conftest import Session


async def test_full_consultation_happy_path(client, operator, doctor):
    s = Session(client, operator)
    start = await s.start()
    assert "mobile number" in start["messages"][0]["content"]
    assert s.ui["stage"] == "identify"

    # 1. Greet and identify (OTP → D365 import)
    await s.identify("98765 43210")
    assert "Priya" in s.last()
    assert s.ui["stage"] == "concern"
    assert any(m["channel"] == "sms" for m in OUTBOX.sent)
    user_lines = [m["content"] for m in s.transcript if m["role"] == "user"]
    assert user_lines == []  # the API returns replies; stored user lines are checked below
    stored = (await client.get(f"/api/conversations/{s.id}", headers=operator)).json()["messages"]
    stored_user = " ".join(m["content"] for m in stored if m["role"] == "user")
    assert "123456" not in stored_user and "9876543210" not in stored_user.replace(" ", "")
    assert "3210" in stored_user  # last 4 digits kept for context

    # 2. Understand the concern
    await s.say("I have dark spots on my cheeks and some acne on my forehead")
    assert s.ui["stage"] == "assess"
    assert s.ui["programme"] == "Acne Care Programme"
    assert s.ui["question"]["id"] == "q01"

    # 3. Assess (Know your Skin)
    await s.answer_all()
    assert s.ui["stage"] == "capture", s.last()
    assert "skin type looks dry" in " ".join(m["content"] for m in s.transcript[-3:])

    # 4. Capture — a bad photo is rejected with guidance, then retaken
    from tests.conftest import face_like_jpeg
    bad = await s.photo("front", face_like_jpeg(9, brightness=40))
    assert bad["photo_ok"] is False
    assert "dark" in bad["messages"][0]["content"].lower()
    assert s.ui["camera"]["pose"] == "front"

    # 5–6. Analyse + recommend run automatically once the photos pass
    out = await s.photos()
    assert out["photo_ok"] is True
    texts = " ".join(m["content"] for m in out["messages"])
    assert "Here's what I found" in texts
    assert "draft" in texts.lower()
    assert s.ui["stage"] == "approval"
    assert s.ui["results_available"] is True

    results = (await client.get(f"/api/conversations/{s.id}/results", headers=operator)).json()
    assert results["plan"]["status"] == "pending_approval"
    assert any(i["catalogue_id"] == "prd-sunscreen" for i in results["plan"]["items"])
    visit_id = results["visit_id"]

    # NFR-03: no summary before approval
    r = await client.get(f"/api/visits/{visit_id}/summary", headers=operator)
    assert r.status_code == 403

    # 7. Approve (doctor)
    queue = (await client.get("/api/doctor/queue", headers=doctor)).json()
    assert queue["counts"]["approvals"] == 1
    r = await client.post(f"/api/visits/{visit_id}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert r.status_code == 200, r.text
    approved_msgs = r.json()["conversation_messages"]
    assert "approved your plan" in approved_msgs[0]["content"] and "book" in approved_msgs[0]["content"]
    assert "earliest times are" in approved_msgs[1]["content"]

    # 8. Book the plan's treatments into a suitable procedure room (FR-19)
    conv = await s.refresh()
    assert conv["ui"]["stage"] == "booking"
    offer = conv["ui"]["booking"]
    assert len(offer["slots"]) == 3 and conv["ui"]["quick_replies"][-2:] == ["Later dates", "Book later"]
    await s.book_all()
    booked = [m["content"] for m in s.transcript if m["content"].startswith("I've held ")]
    assert booked and "provisional until the clinic team confirms it" in booked[0], s.transcript[-3:]
    assert "Your times are held" in s.last() and "Do you have any questions?" in s.last()

    # 9. Wrap up: summary shared, notes saved, next session set, visit completed
    conv = await s.refresh()
    assert conv["ui"]["stage"] == "done"
    assert conv["ui"]["summary_available"] is True
    summary = (await client.get(f"/api/visits/{visit_id}/summary", headers=operator)).json()
    assert summary["approved_by"]["registration_no"] == "DMC/R/12345"
    assert summary["next_session_date"]
    appts = summary["appointments"]
    assert appts and all(a["status"] == "tentative" and a["room_name"] for a in appts)  # chat bookings wait for staff
    assert summary["next_session_date"] == min(a["starts_at"] for a in appts)[:10]  # first treatment = next visit
    sms = [m for m in OUTBOX.sent if "/s/" in m["body"]]
    assert len(sms) == 1
    assert appts[0]["procedure"] in sms[0]["body"] and "(to be confirmed)" in sms[0]["body"]
    token = sms[0]["body"].rsplit("/s/", 1)[1].split()[0]
    public = await client.get(f"/api/public/summaries/{token}")
    assert public.status_code == 200 and public.json()["plan"]

    async with sessionmaker()() as db:
        visit = await db.get(Visit, visit_id)
        assert visit.status == "completed"
        notes = (await db.execute(select(Note).where(Note.visit_id == visit.id))).scalars().all()
        assert notes and "acne" in notes[0].tags
        actions = (await db.execute(select(AIAction).where(AIAction.conversation_id == s.id))).scalars().all()
        names = {a.action for a in actions}
        assert {"send_otp", "verify_otp", "find_or_import_client", "start_visit", "save_assessment", "upload_photo",
                "run_analysis", "get_recommendations", "draft_prescription", "request_approval", "add_note",
                "share_summary", "set_next_session", "offer_ot_slots", "book_ot_slot"} <= names
        # NFR-02: every action has inputs, a result and a reason
        assert all(a.reason and a.result is not None for a in actions)
        # ...but no secrets: OTP codes and full mobile numbers are not in the audit log
        import json as _json
        logged = _json.dumps([a.inputs for a in actions])
        assert "123456" not in logged and "9876543210" not in logged


async def test_new_client_profile_capture(client, operator):
    s = Session(client, operator)
    await s.start()
    await s.identify("91234 56780")
    assert "full name" in s.last()
    await s.say("My name is meera iyer")
    assert s.ui["input"] == "date"
    await s.say("14-02-1995")
    assert s.ui["quick_replies"] == ["Female", "Male", "Other"]
    await s.say("Female")
    assert "Meera" in s.last()
    assert s.ui["stage"] == "concern"


async def test_wrong_otp_then_right(client, operator):
    s = Session(client, operator)
    await s.start()
    await s.say("9876543210")
    await s.say("111111")
    assert "didn't match" in s.last()
    await s.say("123456")
    assert s.ui["stage"] == "concern"


async def test_consent_required(client, operator):
    r = await client.post("/api/conversations", json={"consent_ai": True, "consent_recording": False}, headers=operator)
    assert r.status_code == 400


async def test_message_exactly_once(client, operator):
    s = Session(client, operator)
    await s.start()
    first = await s.say("9876543210", cmid="fixed-message-id-1")
    again = await s.say("9876543210", cmid="fixed-message-id-1")
    assert again.get("duplicate") is True
    assert [m["id"] for m in again["messages"]] == [m["id"] for m in first["messages"]]
    assert sum(1 for m in OUTBOX.sent if m["channel"] == "sms") == 1  # OTP sent once


async def test_returning_client_compares_and_reuses_answers(client, operator, doctor):
    async def visit(seed: int) -> str:
        s = Session(client, operator)
        await s.start()
        await s.identify()
        await s.say("I want to treat my acne")
        await s.answer_all()
        await s.photos(seed)
        res = (await client.get(f"/api/conversations/{s.id}/results", headers=operator)).json()
        r = await client.post(f"/api/visits/{res['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
        assert r.status_code == 200
        await s.book_all()
        return s

    first = await visit(1)
    second = Session(client, operator)
    await second.start()
    await second.identify()
    assert "Welcome back" in second.last()
    await second.say("Following up on my acne")
    assert "kept" in " ".join(m["content"] for m in second.transcript[-2:])  # stable answers reused
    assert second.ui["session_number"] == 2
    await second.answer_all()
    out = await second.photos(7)
    assert "Compared with your visit" in " ".join(m["content"] for m in out["messages"])
    assert first.id != second.id
