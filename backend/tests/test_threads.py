"""Follow-up chats: clients chat about past consultations (phone and clinic device); doctors and admins chat with any
client. Privacy, hand-over to the clinic team, SMS notices and the AI's limits are checked here."""
import uuid

from app.ai import followup
from app.integrations.messaging import OUTBOX
from tests.conftest import Session

PRIYA = "98765 43210"
ROHAN = "98111 22233"


async def finished_visit(client, operator, doctor, mobile=PRIYA) -> Session:
    s = Session(client, operator)
    await s.start()
    await s.identify(mobile)
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()
    r = await client.post(f"/api/visits/{s.ui['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert r.status_code == 200, r.text
    await s.book_all()
    return s


async def phone_login(client, mobile=PRIYA) -> dict:
    r = await client.post("/api/client-auth/start", json={"mobile": mobile})
    assert r.status_code == 200 and r.json()["sent"]
    r = await client.post("/api/client-auth/verify", json={"mobile": mobile, "code": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def say(client, h, tid, text, base="/api/me"):
    r = await client.post(f"{base}/threads/{tid}/messages", json={"text": text, "client_message_id": uuid.uuid4().hex},
                          headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def open_visit_chat(client, h, visit_id, base="/api/me"):
    r = await client.post(f"{base}/threads", json={"visit_id": visit_id}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ── Signing in on the phone ──────────────────────────────────────────────────
async def test_phone_sign_in_and_role_separation(client, operator, doctor):
    await finished_visit(client, operator, doctor)
    OUTBOX.sent.clear()
    # An unknown number gets the same answer and no text is sent.
    r = await client.post("/api/client-auth/start", json={"mobile": "99999 00001"})
    assert r.status_code == 200 and r.json()["sent"] and OUTBOX.sent == []
    r = await client.post("/api/client-auth/verify", json={"mobile": "99999 00001", "code": "123456"})
    assert r.status_code == 400

    r = await client.post("/api/client-auth/start", json={"mobile": PRIYA})
    assert len(OUTBOX.sent) == 1
    r = await client.post("/api/client-auth/verify", json={"mobile": PRIYA, "code": "000000"})
    assert r.status_code == 400
    h = await phone_login(client)
    me = (await client.get("/api/me", headers=h)).json()
    assert me["first_name"] == "Priya" and me["mobile"].endswith("3210") and "98765" not in me["mobile"]
    hist = (await client.get("/api/me/history", headers=h)).json()
    assert len(hist["visits"]) == 1 and hist["visits"][0]["plan"]

    # A client's sign-in never opens staff screens, and staff tokens don't open the client's pages.
    assert (await client.get("/api/doctor/queue", headers=h)).status_code == 403
    assert (await client.get("/api/clients", headers=h)).status_code == 403
    assert (await client.get("/api/me", headers=operator)).status_code == 403
    assert (await client.get("/api/me")).status_code == 401


# ── The assistant answers from the approved plan ─────────────────────────────
async def test_client_asks_about_a_past_consultation(client, operator, doctor):
    s = await finished_visit(client, operator, doctor)
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    out = await say(client, h, tid, "What's my plan again?")
    assert [m["role"] for m in out["messages"]] == ["client", "assistant"]
    reply = out["messages"][-1]["content"]
    assert "doctor-approved plan" in reply and "•" in reply
    assert out["thread"]["mode"] == "ai" and not out["thread"]["waiting"]

    out = await say(client, h, tid, "When is my next appointment?")
    assert "upcoming treatments" in out["messages"][-1]["content"].lower()

    threads = (await client.get("/api/me/threads", headers=h)).json()["threads"]
    assert len(threads) == 1 and threads[0]["unread"] == 0 and threads[0]["title"].startswith("Acne Care Programme")
    # Nothing waits for staff
    q = (await client.get("/api/doctor/queue", headers=doctor)).json()
    assert q["counts"]["messages"] == 0


async def test_unapproved_visit_details_stay_hidden(client, operator, doctor):
    s = Session(client, operator)
    await s.start()
    await s.identify()
    await s.say("I have acne on my forehead")
    await s.answer_all()
    await s.photos()  # draft waits for a doctor
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    out = await say(client, h, tid, "Which products are in my plan?")
    assert "doesn't have a doctor-approved plan" in out["messages"][-1]["content"]
    assert out["thread"]["mode"] == "staff" and out["thread"]["waiting"]


async def test_change_requests_go_to_the_clinic_team(client, operator, doctor):
    s = await finished_visit(client, operator, doctor)
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    out = await say(client, h, tid, "Can I reschedule my laser session to next week?")
    assert "can't change your plan or appointments" in out["messages"][-1]["content"]
    assert out["thread"]["waiting"]
    item = [i for i in (await client.get("/api/doctor/queue", headers=doctor)).json()["items"] if i["type"] == "message"][0]
    assert item["thread_id"] == tid and item["trigger"] == "out_of_scope" and not item["urgent"]


# ── Hand-over, staff replies and texts ───────────────────────────────────────
async def test_symptom_goes_to_a_doctor_who_replies(client, operator, doctor):
    s = await finished_visit(client, operator, doctor)
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    out = await say(client, h, tid, "My skin is burning and swollen after the peel")
    assert "flagged it to a doctor" in out["messages"][-1]["content"]
    assert out["thread"]["mode"] == "staff"

    q = (await client.get("/api/doctor/queue", headers=doctor)).json()
    item = q["items"][0]  # urgent chats sit with escalations at the top
    assert item["type"] == "message" and item["urgent"] and item["client_name"].startswith("Priya")

    staff_view = (await client.get(f"/api/messages/threads/{tid}", headers=doctor)).json()
    assert staff_view["thread"]["handover_reason"] == "medical_concern"
    assert [m["role"] for m in staff_view["messages"]] == ["client", "assistant"]

    # While the clinic team has it, the AI stays quiet.
    out = await say(client, h, tid, "Hello? It still hurts")
    assert [m["role"] for m in out["messages"]] == ["client"]

    OUTBOX.sent.clear()
    r = await client.post(f"/api/messages/threads/{tid}/messages", json={"text": "Please rinse with cool water. "
                          "Can you come in today at 4?"}, headers=doctor)
    assert r.status_code == 200 and r.json()["notified"]
    assert len(OUTBOX.sent) == 1 and "sent you a message" in OUTBOX.sent[0]["body"]
    r = await client.post(f"/api/messages/threads/{tid}/messages", json={"text": "Bring the products you used."},
                          headers=doctor)
    assert not r.json()["notified"] and len(OUTBOX.sent) == 1  # one text until the client reads
    assert (await client.get("/api/doctor/queue", headers=doctor)).json()["counts"]["messages"] == 0

    view = (await client.get(f"/api/me/threads/{tid}", headers=h)).json()
    doc_msgs = [m for m in view["messages"] if m["role"] == "staff"]
    assert len(doc_msgs) == 2 and doc_msgs[0]["author"].startswith("Dr") and doc_msgs[0]["author_role"] == "doctor"
    assert (await client.get("/api/me", headers=h)).json()["unread"] == 0  # reading marks them read

    out = await say(client, h, tid, "Yes, I can come at 4")
    assert [m["role"] for m in out["messages"]] == ["client"]
    r = await client.post(f"/api/messages/threads/{tid}/messages", json={"text": "See you then."}, headers=doctor)
    assert r.json()["notified"]  # they had read the earlier ones, so this one is texted

    r = await client.post(f"/api/messages/threads/{tid}/hand-back", headers=doctor)
    assert r.status_code == 200 and r.json()["thread"]["mode"] == "ai"
    out = await say(client, h, tid, "Thanks!")
    assert out["messages"][-1]["role"] == "assistant"


async def test_admin_starts_a_chat_with_any_client(client, operator, doctor, admin):
    s = await finished_visit(client, operator, doctor)
    clients = (await client.get("/api/clients?q=Priya", headers=admin)).json()
    r = await client.post("/api/messages/threads", json={"client_id": clients[0]["id"]}, headers=admin)
    assert r.status_code == 200
    tid = r.json()["id"]
    assert r.json()["title"] == "Messages with the clinic"
    OUTBOX.sent.clear()
    r = await client.post(f"/api/messages/threads/{tid}/messages", json={"text": "Hi Priya, how is your skin?"}, headers=admin)
    assert r.status_code == 200 and r.json()["message"]["author_role"] == "clinic team"
    assert len(OUTBOX.sent) == 1

    # The client sees it on the clinic device on their next visit (badge in the session, chat under History) ...
    k = Session(client, operator)
    await k.start()
    await k.identify()
    assert k.ui["unread_messages"] == 1
    lst = (await client.get(f"/api/conversations/{k.id}/threads", headers=operator)).json()["threads"]
    assert lst[0]["id"] == tid and lst[0]["unread"] == 1
    view = (await client.get(f"/api/conversations/{k.id}/threads/{tid}", headers=operator)).json()
    assert view["messages"][0]["content"] == "Hi Priya, how is your skin?"
    out = await say(client, operator, tid, "Much better, thank you", base=f"/api/conversations/{k.id}")
    assert out["messages"][0]["channel"] == "clinic device"
    await k.refresh()
    assert k.ui["unread_messages"] == 0

    # ... and the staff list shows it, unread for staff
    st = (await client.get("/api/messages/threads", headers=doctor)).json()["threads"]
    assert st[0]["id"] == tid and st[0]["unread"] == 1 and st[0]["client"]["full_name"].startswith("Priya")
    assert st[0]["waiting"] and st[0]["handover_reason"] == "client_replied"
    # Staff can also open a chat about one of the client's visits
    r = await client.post("/api/messages/threads", json={"client_id": clients[0]["id"], "visit_id": s.ui["visit_id"]},
                          headers=doctor)
    assert r.status_code == 200 and r.json()["visit"]["id"] == s.ui["visit_id"]
    # Operators (the clinic device) can't use the staff chat screens
    assert (await client.get("/api/messages/threads", headers=operator)).status_code == 403


# ── Privacy ──────────────────────────────────────────────────────────────────
async def test_clients_never_reach_each_others_chats(client, operator, doctor):
    priya = await finished_visit(client, operator, doctor)
    hp = await phone_login(client, PRIYA)
    tid = await open_visit_chat(client, hp, priya.ui["visit_id"])
    await say(client, hp, tid, "What's my plan?")

    rohan = Session(client, operator)
    await rohan.start()
    await rohan.identify(ROHAN)
    hr = await phone_login(client, ROHAN)
    assert (await client.get(f"/api/me/threads/{tid}", headers=hr)).status_code == 404
    assert (await client.post(f"/api/me/threads/{tid}/messages", json={"text": "hi"}, headers=hr)).status_code == 404
    assert (await client.post("/api/me/threads", json={"visit_id": priya.ui["visit_id"]}, headers=hr)).status_code == 404
    assert (await client.get(f"/api/me/visits/{priya.ui['visit_id']}/summary", headers=hr)).status_code == 404
    assert (await client.get("/api/me/threads", headers=hr)).json()["threads"] == []
    # On the clinic device too
    base = f"/api/conversations/{rohan.id}"
    assert (await client.get(f"{base}/threads/{tid}", headers=operator)).status_code == 404
    assert (await client.get(f"{base}/threads", headers=operator)).json()["threads"] == []
    # Before verification, no chats at all
    fresh = Session(client, operator)
    await fresh.start()
    assert (await client.post(f"/api/conversations/{fresh.id}/threads", json={}, headers=operator)).status_code == 403


async def test_retried_send_is_stored_once(client, operator, doctor):
    s = await finished_visit(client, operator, doctor)
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    body = {"text": "What's my plan?", "client_message_id": "abc123"}
    a = (await client.post(f"/api/me/threads/{tid}/messages", json=body, headers=h)).json()
    b = (await client.post(f"/api/me/threads/{tid}/messages", json=body, headers=h)).json()
    assert [m["id"] for m in a["messages"]] == [m["id"] for m in b["messages"]]
    view = (await client.get(f"/api/me/threads/{tid}", headers=h)).json()
    assert len(view["messages"]) == 2


# ── With a language model ────────────────────────────────────────────────────
class FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.seen: list = []

    async def chat(self, messages, tools=None, temperature=0.3):
        self.seen.append(messages)
        return {"role": "assistant", "content": self.content}


async def test_llm_answers_from_facts_and_cannot_claim_changes(client, operator, doctor, monkeypatch):
    s = await finished_visit(client, operator, doctor)
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    fake = FakeLLM('{"reply": "Use the gel at night. I have moved your laser session to Friday.", "handover": false}')
    monkeypatch.setattr(followup, "get_provider", lambda: fake)
    out = await say(client, h, tid, "How do I use the gel?")
    assert out["messages"][-1]["content"] == "Use the gel at night."
    facts = fake.seen[0][0]["content"]
    assert "FACTS" in facts and "Acne Care Programme" in facts and "98765" not in facts

    fake.content = '{"reply": "A doctor should look at that, so I have passed it to the clinic team.", "handover": true, ' \
                   '"reason": "dose question"}'
    out = await say(client, h, tid, "Can I use it twice a day instead?")
    assert out["thread"]["waiting"]
    view = (await client.get(f"/api/messages/threads/{tid}", headers=doctor)).json()["thread"]
    assert view["handover_reason"] in ("ai_requested", "out_of_scope")


async def test_symptoms_never_reach_the_llm(client, operator, doctor, monkeypatch):
    s = await finished_visit(client, operator, doctor)
    h = await phone_login(client)
    tid = await open_visit_chat(client, h, s.ui["visit_id"])
    fake = FakeLLM('{"reply": "That is normal, keep going.", "handover": false}')
    monkeypatch.setattr(followup, "get_provider", lambda: fake)
    out = await say(client, h, tid, "There is pus and it is bleeding")
    assert fake.seen == [] and "flagged it to a doctor" in out["messages"][-1]["content"]
