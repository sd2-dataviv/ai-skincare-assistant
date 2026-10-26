"""Conversational LLM layer with a fake provider (no network).

The model decides what the client meant and writes a short human reply; code acts on that decision and adds the next
step verbatim. Covers what went wrong in live testing: identity/OTP never reach the model, burns and injuries go to a
doctor, apologies get a human reply, answers are never guessed, photos can't be faked, and the model sees the client's
memory (name, concern, earlier answers, previous visits).
"""
import json

from sqlalchemy import select

from app.ai import agents
from app.ai.providers import LLMError
from app.db import sessionmaker
from app.models import AIAction, Escalation
from tests.conftest import Session


class FakeProvider:
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, temperature=0.3):
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("LLM was called but no response was scripted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def decide(intent, reply="", value=""):
    return {"role": "assistant", "content": json.dumps({"intent": intent, "value": value, "reply": reply})}


async def _start(client, operator, monkeypatch, fake):
    monkeypatch.setattr(agents, "get_provider", lambda: fake)
    s = Session(client, operator)
    await s.start()
    return s


async def _to_assess(client, operator, monkeypatch, fake):
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify()
    await s.say("acne on my face")  # scripted path below via the "concern" decision in each test's script
    return s


async def test_identity_and_otp_never_reach_the_llm(client, operator, monkeypatch):
    fake = FakeProvider([])
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify("98765 43210")
    assert s.ui["stage"] == "concern" and "Priya" in s.last()
    assert fake.calls == []


async def test_concern_is_understood_and_acknowledged(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", "That sounds frustrating, Priya, chin breakouts are really common.",
                                "breakouts on chin, dark patches")])
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify()
    out = await s.say("so basically my chin keeps breaking out and there are dark patches")
    texts = [m["content"] for m in out["messages"]]
    assert texts[0].startswith("That sounds frustrating, Priya") and "Acne Care Programme" in texts[0]
    assert texts[1].startswith("A few hours after washing your face")  # verbatim question, no "Question 1 of 18"
    async with sessionmaker()() as db:
        act = (await db.execute(select(AIAction).where(AIAction.action == "start_visit"))).scalar_one()
        assert act.reason.startswith("Client described their concern")


async def test_burn_goes_to_a_doctor_not_a_skincare_programme(client, operator, monkeypatch):
    fake = FakeProvider([{"role": "assistant", "content": "I'm sorry, that sounds painful."}])  # empathy line
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify()
    await s.say("burn on hand")
    assert s.ui["mode"] == "handed_over"
    assert "needs a doctor" in s.last()
    async with sessionmaker()() as db:
        esc = (await db.execute(select(Escalation))).scalars().all()
        assert [e.trigger for e in esc] == ["medical_concern"]


async def test_burn_mid_questionnaire_is_not_saved_as_an_answer(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", "Thanks for telling me."),
                         {"role": "assistant", "content": "Oh no, I'm sorry to hear that."}])
    s = await _to_assess(client, operator, monkeypatch, fake)
    assert s.ui["question"]["id"] == "q01"
    await s.say("burn on face")
    assert s.ui["mode"] == "handed_over"
    async with sessionmaker()() as db:
        saves = (await db.execute(select(AIAction).where(AIAction.action == "save_assessment"))).scalars().all()
        assert saves == []


async def test_sun_answer_with_burn_is_not_an_injury(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", "")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    for _ in range(6):  # tap through to the sun question
        await s.say(s.ui["quick_replies"][0])
    assert s.ui["question"]["id"] == "q07"
    await s.say("Burns easily, rarely tans")
    assert s.ui["mode"] == "ai" and s.ui["question"]["id"] == "q08"


async def test_apology_gets_a_human_reply_then_the_question(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("other", "No worries at all, take your time.")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    out = await s.say("sorry")
    texts = [m["content"] for m in out["messages"]]
    assert texts[0] == "No worries at all, take your time."
    assert texts[1].startswith("A few hours after washing your face")
    assert s.ui["question"]["id"] == "q01"


async def test_free_text_answer_is_saved_with_a_natural_reply(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("answer", "An oily T-zone is really common.", "tzone_shiny")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    out = await s.say("it gets greasy in the middle of my face by lunch")
    assert out["messages"][0]["content"].startswith("An oily T-zone is really common. By midday")
    assert s.ui["question"]["id"] == "q02"


async def test_model_cannot_answer_a_different_question_or_invent_values(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("answer", "Okay.", "pregnant")])  # not an option of q01
    s = await _to_assess(client, operator, monkeypatch, fake)
    await s.say("hmm whatever you think")
    assert s.ui["question"]["id"] == "q01" and s.ui["mode"] == "ai"


async def test_tapped_options_skip_the_model(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", "")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    n = len(fake.calls)
    await s.say(s.ui["quick_replies"][2])
    assert len(fake.calls) == n
    assert "By midday, how shiny does your face look?" in s.last()


async def test_memory_reaches_the_model(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("other", "Of course.")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    await s.say(s.ui["quick_replies"][0])  # q01 answered by tap
    await s.say("can you remind me what I told you?")
    system = fake.calls[-1][0]["content"]
    assert "Priya" in system and "acne on my face" in system and "Tight or dry" in system
    assert "123456" not in json.dumps(fake.calls) and "9876543210" not in json.dumps(fake.calls)


async def test_typing_cannot_fake_photos(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("other", "Great, now take the left side photo.")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    await s.answer_all()
    assert s.ui["stage"] == "capture"
    out = await s.say("done, I took them all")
    assert s.ui["camera"]["missing"] == ["front", "left", "right"]
    assert "front photo" in out["messages"][-1]["content"]
    assert all("left side photo" not in m["content"] for m in out["messages"])  # model's wrong claim dropped
    assert out["messages"][0]["content"] == "I don't have the front photo yet."


async def test_question_about_photos_is_still_answered(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""),
                         decide("other", "The photos let the analysis measure acne and pigmentation accurately.")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    await s.answer_all()
    out = await s.say("why do you need photos?")
    assert out["messages"][0]["content"].startswith("The photos let the analysis")
    assert "front photo" in out["messages"][-1]["content"]


async def test_model_restating_the_question_is_not_shown_twice(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("other", "No worries, Priya. How does your skin feel? You can "
                                                                 "pick Tight or dry, Comfortable, or Shiny all over.")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    out = await s.say("sorry")
    texts = [m["content"] for m in out["messages"]]
    assert texts[0] == "No worries, Priya."
    assert texts[1].startswith("A few hours after washing your face") and len(texts) == 2


async def test_garbled_or_broken_model_output_never_shown(client, operator, monkeypatch):
    garbled = {"role": "assistant", "content": "{\"intent\": \"other\", \"reply\": \"Do you ​​\\n\\n…\\n\\nOops… …\\n\\n…\"}"}
    fake = FakeProvider([garbled])
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify()
    out = await s.say("hmm not sure")
    assert all("​" not in m["content"] and "Oops" not in m["content"] for m in out["messages"])
    assert out["messages"][-1]["content"] == "What would you like help with today? Tell me in your own words."


async def test_llm_failure_falls_back_to_scripted(client, operator, monkeypatch):
    fake = FakeProvider([LLMError("down")])
    s = await _start(client, operator, monkeypatch, fake)
    await s.identify()
    await s.say("I have acne on my forehead")
    assert s.ui["stage"] == "assess"


async def test_finished_visit_and_a_new_number(client, operator, monkeypatch):
    from app.ai.agents import ScriptedAgent

    assert "already finished" in (await ScriptedAgent()._done(None, None, "8934071154"))[0]


def test_clean_reply_keeps_real_spaces():
    assert agents.clean_reply("Dr Kavya Rao on 23 October 2026​.") == "Dr Kavya Rao on 23 October 2026."
    assert agents.clean_reply("Fine, **thanks**.") == "Fine, thanks."


def test_parse_decision_tolerates_noise():
    assert agents.parse_decision('Sure! {"intent": "answer", "value": "x", "reply": "ok"} thanks')["value"] == "x"
    assert agents.parse_decision("no json here")["intent"] == "other"


async def test_memory_question_is_not_mistaken_for_wanting_a_person(client, operator, monkeypatch):
    fake = FakeProvider([decide("concern", ""), decide("human", "You mentioned acne on your face, Priya.")])
    s = await _to_assess(client, operator, monkeypatch, fake)
    out = await s.say("hmm what was it I told you my problem was?")
    assert s.ui["mode"] == "ai"
    assert out["messages"][0]["content"] == "You mentioned acne on your face, Priya."
    assert out["messages"][1]["content"].startswith("A few hours after washing your face")
