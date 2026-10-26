"""The clinic's skin models over WebSocket: scores from the diagnosis model's findings, overlays and the post-treatment
preview stored for the visit, who may see them (doctor any time; client only after approval), and graceful fallback
when a model server is old, down or finds nothing. The model servers are fakes speaking the same protocol."""
import json

import cv2
import numpy as np
import pytest
from websockets.asyncio.server import serve

from app.config import get_settings
from app.integrations import skin_models as sm
from app.integrations.analysis_engine import SkinModelEngine, set_analysis_engine
from app.services import model_images
from tests.conftest import Session
from tests.test_threads import phone_login


def jpeg(color=(120, 140, 170), size=512) -> bytes:
    img = np.full((size, size, 3), color, np.uint8)
    cv2.rectangle(img, (40, 40), (90, 90), (0, 0, 255), 2)
    return cv2.imencode(".jpg", img)[1].tobytes()


DIAG_FRONT = {
    "face_detected": True, "face_box": [100, 50, 700, 750], "image_size": [1024, 1024], "output_size": [512, 512],
    "detections": [{"class": "acne", "score": 0.8, "bbox": [10, 10, 30, 30], "area_px": 400} for _ in range(6)]
    + [{"class": "pigmentation", "score": 0.6, "bbox": [100, 100, 300, 200], "area_px": 20000} for _ in range(2)]
    + [{"class": "dark circles", "score": 0.7, "bbox": [300, 400, 450, 470], "area_px": 8000} for _ in range(2)]
    + [{"class": "scars", "score": 0.5, "bbox": [500, 500, 520, 520], "area_px": 300}],
}
SIDE_NO_FACE = {"face_detected": False, "face_box": None, "detections": []}


class FakeModel:
    """mode: new (image + findings when asked) | old (image only) | noface | nothing (preview found nothing)."""

    def __init__(self, kind: str, mode: str = "new"):
        self.kind, self.mode, self.calls, self.params = kind, mode, 0, []

    async def handler(self, ws):
        async for msg in ws:
            if isinstance(msg, str):
                self.params.append(json.loads(msg))
                continue
            self.calls += 1
            await ws.send(jpeg())
            p = self.params[-1]
            inner = p.get("parameters", p)
            if self.mode == "old" or not inner.get("return_predictions"):
                continue
            if self.kind == "diag":
                d = DIAG_FRONT if self.calls % 3 == 1 and self.mode != "noface" else SIDE_NO_FACE
            else:
                found = self.mode != "nothing"
                d = {"face_detected": self.mode != "noface", "face_box": [100, 50, 700, 750], "image_size": [512, 512],
                     "detections": DIAG_FRONT["detections"][:3] if found else [], "inpainted": found}
            await ws.send(json.dumps({"type": "predictions", "model": self.kind, **d}))


@pytest.fixture
async def models(monkeypatch):
    s = get_settings()
    servers = []

    async def start(diag: FakeModel | None = None, post: FakeModel | None = None, down: bool = False):
        urls = {}
        for name, fake in (("diag", diag), ("post", post)):
            if fake is None:
                continue
            srv = await serve(fake.handler, "127.0.0.1", 0).__aenter__()
            servers.append(srv)
            urls[name] = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/ws/cv/1/image/image"
        if down:  # nothing listening on these ports
            urls = {"diag": "ws://127.0.0.1:9/ws/cv/1/image/image", "post": "ws://127.0.0.1:9/ws/cv/2/image/image"}
        monkeypatch.setattr(s, "skin_diagnosis_ws_url", urls.get("diag"))
        monkeypatch.setattr(s, "skin_post_treatment_ws_url", urls.get("post"))
        monkeypatch.setattr(s, "skin_model_details_wait_s", 0.4)
        monkeypatch.setattr(s, "skin_model_connect_timeout_s", 2.0)
        sm.reset_services()
        set_analysis_engine(SkinModelEngine())

    yield start
    await model_images.drain()
    for srv in servers:
        srv.close()
        await srv.wait_closed()
    sm.reset_services()


async def consult(client, operator, mobile="98765 43210") -> Session:
    s = Session(client, operator)
    await s.start()
    await s.identify(mobile)
    await s.say("I have acne and dark circles")
    await s.answer_all()
    await s.photos()
    return s


async def detail(client, doctor, vid) -> dict:
    r = await client.get(f"/api/visits/{vid}", headers=doctor)
    assert r.status_code == 200, r.text
    return r.json()


def diagnosis(d: dict) -> dict:
    return [a for a in d["analyses"] if a["kind"] == "diagnosis"][-1]


# ── Unit ─────────────────────────────────────────────────────────────────────
def test_return_predictions_goes_inside_the_wrapper():
    wrapped = sm.with_return_predictions({"parameters": {"threshold": 0.4}})
    assert wrapped == {"parameters": {"threshold": 0.4, "return_predictions": True}}
    assert sm.with_return_predictions({"threshold": 0.1}) == {"threshold": 0.1, "return_predictions": True}


def test_scores_from_findings_take_the_worst_photo_and_keep_scars_aside():
    out = sm.summarise({"front": DIAG_FRONT, "left": SIDE_NO_FACE},
                       ["acne", "scars", "pigmentation", "dark circles"])
    assert out["poses_used"] == ["front"]
    acne, pig, dc = (out["scores"][k] for k in ("acne", "pigmentation", "dark_circles"))
    assert acne["count"] == 6 and 35 < acne["score"] < 45  # mild
    assert pig["count"] == 2 and pig["area_pct"] == pytest.approx(3.81, abs=0.01)
    assert 45 <= dc["score"] < 70  # moderate
    assert out["extras"]["scars"]["count"] == 1 and "scars" not in out["scores"]
    clear = sm.summarise({"front": {**DIAG_FRONT, "detections": []}}, ["acne"])
    assert clear["scores"]["acne"]["score"] < 25  # a face with nothing found scores minimal
    # A class the service wasn't asked to look for is not scored (absence would not mean clear skin)
    assert "pigmentation" not in sm.summarise({"front": DIAG_FRONT}, ["acne"])["scores"]


def test_gateway_token_goes_into_the_address_and_never_into_messages(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "skin_model_token", "SECRET.jwt")
    gw = "ws://192.168.0.135:5007/ws/models/187/ws/cv/1001/image/image?token=---------------&notify=1"
    assert sm.resolve_url(gw) == "ws://192.168.0.135:5007/ws/models/187/ws/cv/1001/image/image?token=SECRET.jwt&notify=1"
    assert sm.resolve_url("ws://h/x?t={token}") == "ws://h/x?t=SECRET.jwt"
    # an address that doesn't ask for a token never gets one
    assert sm.resolve_url("ws://h:9601/ws/cv/1/image/image") == "ws://h:9601/ws/cv/1/image/image"
    assert sm.resolve_url(gw, "FROM.SESSION") == gw.replace("---------------", "FROM.SESSION")
    assert "SECRET" not in sm._redact("failed for ws://h/x?token=SECRET.jwt")
    assert "OTHER" not in sm._redact("failed for ws://h/x?token=OTHER.jwt", "OTHER.jwt")
    monkeypatch.setattr(s, "skin_model_token", None)
    assert sm.resolve_url(gw) == gw


def test_progress_notes_and_errors_from_the_gateway():
    assert sm._note('{"type": "notify", "message": "queued"}') is None
    assert sm._note('{"status": "error", "message": "model not loaded"}') == "model not loaded"
    assert sm._note('{"error": "invalid token"}') == "invalid token"


async def test_gateway_refusing_the_token_is_explained(monkeypatch):
    async def refuse(connection, request):
        return connection.respond(403, "Forbidden\n")

    srv = await serve(lambda ws: None, "127.0.0.1", 0, process_request=refuse).__aenter__()
    try:
        s = get_settings()
        monkeypatch.setattr(s, "skin_model_token", "EXPIRED.jwt")
        url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/ws/models/187/ws/cv/1001/image/image?token=x&notify=1"
        sm.reset_services()
        svc = sm.ServiceConfig("skin diagnosis model", url, '{"parameters": {}}', 5)
        with pytest.raises(sm.ModelUnavailable) as e:
            await sm.call_service(svc, jpeg())
        assert "HTTP 403" in str(e.value) and "could not be renewed" in str(e.value) and "EXPIRED" not in str(e.value)
        assert svc._down_until == 0  # a token problem: the server isn't skipped for the next call
    finally:
        srv.close()
        await srv.wait_closed()


async def test_notes_before_and_after_the_image_are_skipped():
    async def chatty(ws):
        await ws.recv(); await ws.recv()  # noqa: E702
        await ws.send('{"type": "notify", "message": "queued"}')
        await ws.send(jpeg((90, 110, 150)))  # a processed picture, not the photo sent
        await ws.send('{"type": "notify", "message": "done"}')
        await ws.send(json.dumps({"type": "predictions", **DIAG_FRONT}))
        await ws.wait_closed()

    srv = await serve(chatty, "127.0.0.1", 0).__aenter__()
    try:
        svc = sm.ServiceConfig("skin diagnosis model", f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/", "{}", 5)
        reply = await sm.call_service(svc, jpeg())
        assert reply.image[:2] == b"\xff\xd8" and reply.details["face_detected"] is True
    finally:
        srv.close()
        await srv.wait_closed()


async def test_a_photo_sent_back_unchanged_is_a_failure():
    async def echo(ws):
        await ws.recv()
        await ws.send(await ws.recv())
        await ws.wait_closed()

    srv = await serve(echo, "127.0.0.1", 0).__aenter__()
    try:
        svc = sm.ServiceConfig("skin post-treatment model", f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/", "{}", 5)
        with pytest.raises(sm.ModelUnavailable, match="unchanged"):
            await sm.call_service(svc, jpeg())
    finally:
        srv.close()
        await srv.wait_closed()


# ── Flow ─────────────────────────────────────────────────────────────────────
async def test_models_score_the_visit_and_the_client_sees_pictures_only_after_approval(client, operator, doctor, models):
    diag, post = FakeModel("diag"), FakeModel("post")
    await models(diag, post)
    s = await consult(client, operator)
    vid = s.ui["visit_id"]
    assert diag.calls == 3 and post.calls <= 1
    assert diag.params[0]["return_predictions"] is True  # sent unwrapped: works on old and updated services
    assert diag.params[0]["project_id"] == 187 and diag.params[0]["threshold"] == 0.4

    d = await detail(client, doctor, vid)
    dg = diagnosis(d)
    assert dg["engine"] == "skin-model-v1"
    found = dg["result"]["diagnosis"]
    assert found["acne"]["source"] == "model" and found["acne"]["count"] == 6
    assert found["dark_circles"]["source"] == "model"
    assert found["wrinkles"]["source"] == "estimate"  # no model covers it yet
    assert dg["result"]["model"]["status"] == "ok" and dg["result"]["model"]["extras"]["scars"]["count"] == 1

    await model_images.drain()
    imgs = (await detail(client, doctor, vid))["model_images"]
    assert [(i["kind"], i["pose"]) for i in imgs] == [("diagnosis_overlay", "front"), ("diagnosis_overlay", "left"),
                                                      ("diagnosis_overlay", "right"), ("post_treatment_preview", "front")]
    preview = imgs[-1]
    assert preview["status"] == "ready" and preview["changed"] is True and preview["has_before"] and preview["warning"] is None
    assert "not a promise" in preview["caption"]
    overlay = imgs[0]

    # Doctor sees them now; the clinic device and the client's phone don't until the plan is approved
    assert (await client.get(f"/api/model-images/{preview['id']}/file", headers=doctor)).status_code == 200
    r = await client.get(f"/api/model-images/{preview['id']}/file?variant=before", headers=doctor)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (await client.get(f"/api/model-images/{overlay['id']}/file", headers=operator)).status_code == 404
    res = (await client.get(f"/api/conversations/{s.id}/results", headers=operator)).json()
    assert res["images"] == []
    phone = await phone_login(client)
    assert (await client.get(f"/api/me/model-images/{preview['id']}/file", headers=phone)).status_code == 404

    r = await client.post(f"/api/visits/{vid}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert r.status_code == 200

    res = (await client.get(f"/api/conversations/{s.id}/results", headers=operator)).json()
    kinds = [i["kind"] for i in res["images"]]
    # the side photos had no face for the model, so only the front overlay is shown, then the preview
    assert kinds == ["diagnosis_overlay", "post_treatment_preview"] and res["images"][0]["pose"] == "front"
    shown = res["images"][-1]
    assert shown["title"] == "Illustration after treatment" and "Illustration only" in shown["caption"]
    assert (await client.get(f"/api/model-images/{overlay['id']}/file", headers=operator)).status_code == 200
    summary = (await client.get(f"/api/me/visits/{vid}/summary", headers=phone)).json()
    assert [i["id"] for i in summary["images"]] == [i["id"] for i in res["images"]]
    r = await client.get(f"/api/me/model-images/{preview['id']}/file", headers=phone)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"

    # Another client can't open them; a shared summary link can, for its own visit only
    share = (await client.post(f"/api/visits/{vid}/summary/share", json={"channel": "link"}, headers=operator)).json()
    r = await client.get(f"/api/public/summaries/{share['token']}/images/{preview['id']}")
    assert r.status_code == 200
    assert (await client.get(f"/api/public/summaries/not-a-token/images/{preview['id']}")).status_code == 404


async def test_another_clients_phone_cannot_open_the_pictures(client, operator, doctor, models):
    await models(FakeModel("diag"), FakeModel("post"))
    priya = await consult(client, operator)
    await client.post(f"/api/visits/{priya.ui['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
    await model_images.drain()
    rohan = await consult(client, operator, "98111 22233")
    assert rohan.ui["visit_id"]
    imgs = (await detail(client, doctor, priya.ui["visit_id"]))["model_images"]
    rohan_phone = await phone_login(client, "98111 22233")
    for i in imgs:
        assert (await client.get(f"/api/me/model-images/{i['id']}/file", headers=rohan_phone)).status_code == 404


async def test_older_service_without_findings_keeps_the_pictures_and_estimates(client, operator, doctor, models):
    await models(FakeModel("diag", "old"), FakeModel("post", "old"))
    s = await consult(client, operator)
    d = await detail(client, doctor, s.ui["visit_id"])
    dg = diagnosis(d)
    assert dg["engine"] == "skin-model-images+mock" and dg["result"]["model"]["status"] == "no_findings"
    assert all(v["source"] == "estimate" for v in dg["result"]["diagnosis"].values())
    await model_images.drain()
    imgs = (await detail(client, doctor, s.ui["visit_id"]))["model_images"]
    assert len(imgs) == 4 and imgs[-1]["status"] == "ready" and imgs[-1]["changed"] is None
    assert all("can't confirm" in i["warning"] for i in imgs)
    # Without findings we can't be sure what the pictures are, so the client never sees them
    await client.post(f"/api/visits/{s.ui['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
    res = (await client.get(f"/api/conversations/{s.id}/results", headers=operator)).json()
    assert res["images"] == []


async def test_a_reply_of_the_wrong_size_is_flagged_and_kept_from_the_client(client, operator, doctor, models, monkeypatch):
    """E.g. another model answering on the post-treatment address: it sends the whole photo, not the face crop."""
    post = FakeModel("post")
    await models(FakeModel("diag"), post)
    s = get_settings()
    monkeypatch.setattr(s, "skin_post_treatment_params", s.skin_post_treatment_params.replace("[512,512]", "[300,300]"))
    sm.reset_services()
    v = await consult(client, operator)
    await model_images.drain()
    preview = (await detail(client, doctor, v.ui["visit_id"]))["model_images"][-1]
    assert preview["status"] == "ready" and "not the size asked for" in preview["warning"]
    await client.post(f"/api/visits/{v.ui['visit_id']}/approval/decision", json={"decision": "approve"}, headers=doctor)
    res = (await client.get(f"/api/conversations/{v.id}/results", headers=operator)).json()
    assert [i["kind"] for i in res["images"]] == ["diagnosis_overlay"]  # front only (no face in the side photos)


async def test_models_down_the_consultation_carries_on_and_the_doctor_can_retry(client, operator, doctor, models):
    await models(down=True)
    s = await consult(client, operator)
    vid = s.ui["visit_id"]
    assert s.ui["stage"] != "photos"  # the analysis went ahead on estimates
    dg = diagnosis(await detail(client, doctor, vid))
    assert dg["engine"] == "mock-v1" and dg["result"]["model"]["status"] == "unavailable"
    assert "not reachable" in dg["result"]["model"]["message"]
    await model_images.drain()
    imgs = (await detail(client, doctor, vid))["model_images"]
    assert [(i["kind"], i["status"]) for i in imgs] == [("post_treatment_preview", "failed")]
    assert "not reachable" in imgs[0]["error"] or "unreachable" in imgs[0]["error"]

    # The model server comes back: the doctor asks for the preview again
    post = FakeModel("post")
    await models(FakeModel("diag"), post)
    r = await client.post(f"/api/visits/{vid}/model-images/preview", headers=doctor)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert (await client.post(f"/api/visits/{vid}/model-images/preview", headers=operator)).status_code == 403
    await model_images.drain()
    imgs = (await detail(client, doctor, vid))["model_images"]
    assert imgs[-1]["status"] == "ready" and post.calls == 1


async def test_preview_that_changed_nothing_is_not_shown_to_the_client(client, operator, doctor, models):
    await models(FakeModel("diag"), FakeModel("post", "nothing"))
    s = await consult(client, operator)
    vid = s.ui["visit_id"]
    await client.post(f"/api/visits/{vid}/approval/decision", json={"decision": "approve"}, headers=doctor)
    await model_images.drain()
    staff_imgs = (await detail(client, doctor, vid))["model_images"]
    assert staff_imgs[-1]["status"] == "ready" and staff_imgs[-1]["changed"] is False
    res = (await client.get(f"/api/conversations/{s.id}/results", headers=operator)).json()
    assert [i["kind"] for i in res["images"]] == ["diagnosis_overlay"]  # front only (no face in the side photos)


async def test_no_face_found_means_estimates_and_lower_confidence(client, operator, doctor, models):
    await models(FakeModel("diag", "noface"), None)
    s = await consult(client, operator)
    d = await detail(client, doctor, s.ui["visit_id"])
    dg = diagnosis(d)
    assert dg["result"]["model"]["status"] == "no_face"
    assert all(v["source"] == "estimate" for v in dg["result"]["diagnosis"].values())
    assert not [i for i in d["model_images"] if i["kind"] == "post_treatment_preview"]  # no preview model configured


async def test_agent_is_not_told_about_the_preview(client, operator, models):
    await models(FakeModel("diag"), FakeModel("post"))
    s = await consult(client, operator)
    assert "illustration" not in " ".join(m["content"].lower() for m in s.transcript)
