"""The talking avatar relay, against a fake AI Avatar service that answers like the real one (Direct mode).

Checked on the live service on 28 October 2026: a session opened without a mode flag starts in "teacher" mode and
plays a greeting; {"type":"mode","value":"direct"} then {"type":"stop"} silences it, and each plain-text frame after
that is said word for word ({"type":"avatar_reply","mode":"direct"}, the frames, then avatar-speak-completed).
"""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from app.api import avatar as avatar_api
from app.config import get_settings
from app.integrations import avatar
from app.integrations.platform_auth import gateway_tokens
from tests.test_platform_sso import auth  # noqa: F401 - the fake platform auth service fixture

FRAME = b"\x20" + b"\x00" * 64  # a relayed SyncTalk frame


class FakeAvatarService:
    def __init__(self, auth):
        self.auth = auth
        self.accept = lambda tok: tok in auth.issued
        self.paths: list[str] = []
        self.tokens: list[str] = []
        self.received: list[str] = []
        self.close_after_connect: int | None = None

    async def check(self, connection, request):
        q = parse_qs(urlsplit(request.path).query)
        tok = q.get("token", [""])[0]
        self.tokens.append(tok)
        self.paths.append(urlsplit(request.path).path)
        if not self.accept(tok):
            return connection.respond(403, "invalid token\n")
        return None

    async def handler(self, ws):
        await ws.send(json.dumps({"message": "Hello there! I'm Ai-Trainer."}))  # the service's own greeting
        await ws.send(json.dumps({"type": "session_state", "mode": "teacher", "renderer": "synctalk",
                                  "avatar": {"avatar_id": 14, "name": "Meera"}}))
        if self.close_after_connect:
            await ws.close(self.close_after_connect, "nope")
            return
        mode = "teacher"
        async for msg in ws:
            self.received.append(msg)
            try:
                j = json.loads(msg)
            except ValueError:
                j = None
            if isinstance(j, dict):
                if j.get("type") == "mode":
                    mode = j["value"]
                continue
            if mode == "direct":
                await ws.send(json.dumps({"type": "avatar_reply", "mode": "direct", "source": "direct", "message": msg}))
                await ws.send(FRAME)
                await ws.send(json.dumps({"type": "start", "message": "Routing to Workflow Hub..."}))  # not for us
                await ws.send(json.dumps({"type": "avatar-speak-completed"}))


class FakeBrowser:
    def __init__(self, *lines: dict):
        self.inbox: asyncio.Queue = asyncio.Queue()
        for line in lines:
            self.inbox.put_nowait(json.dumps(line))
        self.texts: list[dict] = []
        self.binary: list[bytes] = []
        self.closed: tuple[int, str | None] | None = None
        self.done = asyncio.Event()

    async def receive_text(self) -> str:
        msg = await self.inbox.get()
        if msg == "__closed__":
            raise WebSocketDisconnect(1000)  # what Starlette raises once the browser has gone
        return msg

    async def send_text(self, data: str) -> None:
        self.texts.append(json.loads(data))
        if self.texts[-1].get("type") == "avatar-speak-completed":
            self.done.set()

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.closed is None:
            self.closed = (code, reason)
        self.done.set()
        self.inbox.put_nowait("__closed__")


@pytest.fixture
async def service(auth, monkeypatch):  # noqa: F811
    fake = FakeAvatarService(auth)
    srv = await serve(fake.handler, "127.0.0.1", 0, process_request=fake.check).__aenter__()
    port = srv.sockets[0].getsockname()[1]
    s = get_settings()
    monkeypatch.setattr(s, "avatar_ws_url", f"ws://127.0.0.1:{port}")
    monkeypatch.setattr(s, "skin_model_auth_email", "models@dataviv.in")
    monkeypatch.setattr(s, "skin_model_auth_password", "pw")
    gateway_tokens.reset()
    yield fake
    srv.close()
    await srv.wait_closed()


async def run(browser: FakeBrowser, timeout: float = 5.0, until_done: bool = True):
    relay = avatar.Relay(client=browser, who="operator@clinic.local", avatar_id=14, language="en")
    task = asyncio.create_task(relay.run())
    if until_done:
        await asyncio.wait_for(browser.done.wait(), timeout)
        await browser.close(1000)
    await asyncio.wait_for(task, timeout)
    return relay


def test_speakable_text():
    assert avatar.speakable("**Your plan**\n• Gentle cleanser  \n• See [the summary](https://x.y/s/abc)") == \
        "Your plan Gentle cleanser See the summary"
    assert avatar.speakable("Dr. Kavya Rao will review this plan.") == "Doctor Kavya Rao will review this plan."
    long = "One sentence here. " * 200
    out = avatar.speakable(long)
    assert len(out) <= get_settings().avatar_max_speech_chars and out.endswith(".")


async def test_session_needs_the_avatar_to_be_set_up(client, operator, doctor, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "avatar_ws_url", None)
    assert (await client.post("/api/avatar/session", headers=operator)).status_code == 404
    monkeypatch.setattr(s, "avatar_ws_url", "wss://avatar.example")
    assert (await client.post("/api/avatar/session", headers=doctor)).status_code == 403  # not the consultation screen
    r = await client.post("/api/avatar/session", headers=operator)
    assert r.status_code == 200
    body = r.json()
    assert body["avatar_id"] == 14 and body["path"] == "/api/avatar/ws"
    assert avatar_api._check_ticket(body["ticket"]) is not None
    assert avatar_api._check_ticket(body["ticket"]) is None  # one socket per ticket
    assert avatar_api._check_ticket("garbage") is None
    health = (await client.get("/api/health")).json()
    assert health["avatar"]["avatar_id"] == 14 and "token" not in json.dumps(health["avatar"])


async def test_the_avatar_says_the_assistants_reply_in_direct_mode(service):
    browser = FakeBrowser({"type": "mode", "value": "llm"},  # never relayed: the kiosk can't change the mode
                          {"query": "what is the weather"},  # nor ask the service's own AI
                          {"type": "speak", "text": "**Welcome!** • Let's look at your skin."})
    await run(browser)
    # Opened as the service account's platform user, in Direct mode, with the greeting stopped.
    assert service.paths == ["/81/ws"]
    assert [json.loads(m) for m in service.received[:3]] == [
        {"type": "language", "value": "en"}, {"type": "mode", "value": "direct"},
        {"type": "stop", "action": "stop", "value": "stop"}]
    assert service.received[3:] == ["Welcome! Let's look at your skin."]
    # The browser gets the frames and the player's JSON, never the greeting, routing frames or the token.
    kinds = [t.get("type") for t in browser.texts]
    assert kinds == ["session_state", "avatar_reply", "avatar-speak-completed"]
    assert browser.binary == [FRAME]
    assert all(service.tokens[0] not in json.dumps(t) for t in browser.texts)


async def test_stop_is_relayed(service):
    browser = FakeBrowser({"type": "speak", "text": "Hello."}, {"type": "stop"})
    await run(browser)
    await asyncio.sleep(0.05)
    assert service.received.count(avatar.STOP_FRAME) == 2  # once on connect, once asked


async def test_a_refused_platform_token_is_renewed_once(service):
    first = []

    def accept(tok):
        if not first:
            first.append(tok)
            return False  # e.g. revoked
        return tok in service.auth.issued

    service.accept = accept
    browser = FakeBrowser({"type": "speak", "text": "Hello."})
    await run(browser)
    assert len(service.tokens) == 2 and service.tokens[0] != service.tokens[1]
    assert browser.binary == [FRAME]


async def test_refusals_close_the_browser_socket_with_a_reason(service, monkeypatch):
    service.accept = lambda tok: False
    browser = FakeBrowser()
    await run(browser, until_done=False)
    assert browser.closed[0] == avatar.CLOSE_REFUSED

    s = get_settings()
    monkeypatch.setattr(s, "skin_model_auth_email", None)
    gateway_tokens.reset()
    browser = FakeBrowser()
    await run(browser, until_done=False)
    assert browser.closed[0] == avatar.CLOSE_NO_SESSION and "service account" in browser.closed[1]


async def test_an_unavailable_avatar_and_an_idle_session_are_closed(service, monkeypatch):
    service.close_after_connect = 4404
    browser = FakeBrowser()
    await run(browser, until_done=False)
    assert browser.closed[0] == avatar.CLOSE_UNAVAILABLE

    service.close_after_connect = None
    monkeypatch.setattr(get_settings(), "avatar_idle_close_s", 0.3)
    browser = FakeBrowser()
    await run(browser, until_done=False)
    assert browser.closed[0] == avatar.CLOSE_IDLE


async def test_a_full_device_limit_is_explained_and_not_logged_in_full(service, client, caplog):
    """The platform caps an account at 4 signed-in devices. Its refusal lists the devices (and their addresses): only
    the reason code may reach the log, the health check and the browser."""
    from fastapi.responses import JSONResponse

    from app.integrations import platform_auth

    app = service.auth.app
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/api/users/login"]

    @app.post("/api/users/login")
    def full(body: dict):
        return JSONResponse(status_code=403, content={"detail": {
            "code": "DEVICE_LIMIT_REACHED", "detail": "This account is already signed in on 4 devices.", "max_devices": 4,
            "devices": [{"id": 97, "os": "Linux", "browser": "Chrome", "last_ip_address": "10.9.8.7"}]}})

    browser = FakeBrowser()
    with caplog.at_level("INFO"):
        await run(browser, until_done=False)
    code, reason = browser.closed
    assert code == avatar.CLOSE_NO_SESSION and "DEVICE_LIMIT_REACHED" in reason
    assert "10.9.8.7" not in caplog.text and "DEVICE_LIMIT_REACHED" in caplog.text
    health = (await client.get("/api/health")).json()
    assert "sign out of one" in health["avatar"]["problem"]
    assert "10.9.8.7" not in json.dumps(health)
    platform_auth.last_refusal = None


async def test_voice_only_mode_speaks_in_the_avatars_voice(client, operator, doctor, monkeypatch):
    """When the avatar can't be used, the replies are still said in its voice by the same service's speech engine."""
    import httpx

    s = get_settings()
    seen = []

    def engine(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=b"RIFF\x00\x00\x00\x00WAVEfmt ", headers={"content-type": "audio/wav"})

    monkeypatch.setattr(avatar, "_tts_transport", httpx.MockTransport(engine))
    monkeypatch.setattr(s, "avatar_ws_url", None)
    assert (await client.post("/api/avatar/speech", json={"text": "Hello"}, headers=operator)).status_code == 404
    monkeypatch.setattr(s, "avatar_ws_url", "wss://avatar.example")
    assert (await client.post("/api/avatar/speech", json={"text": "Hello"}, headers=doctor)).status_code == 403

    r = await client.post("/api/avatar/speech", json={"text": "**Dr. Kavya Rao** will review • your plan."}, headers=operator)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")
    url = seen[-1]
    assert (url.scheme, url.host, url.path) == ("https", "avatar.example", "/tts/preview")
    assert url.params["voice"] == "F1" and url.params["lang"] == "en"
    assert url.params["text"] == "Doctor Kavya Rao will review your plan."

    monkeypatch.setattr(avatar, "_tts_transport", httpx.MockTransport(lambda req: httpx.Response(500)))
    r = await client.post("/api/avatar/speech", json={"text": "Hello"}, headers=operator)
    assert r.status_code == 503  # the browser then uses its own voice
    health = (await client.get("/api/health")).json()
    assert health["avatar"]["voice_only_fallback"] == "voice F1"
