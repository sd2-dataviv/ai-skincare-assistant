"""The talking avatar: the clinic's AI Avatar service (wss://avatar.instaviv.in) gives the consultation a face and voice.

The app's own assistant still decides what is said. The avatar session runs in the service's **Direct** mode, where
the avatar says the text it is sent, word for word; the service's own AI (LLM, MCP, course modes) is never used.

The browser never talks to the service directly. It opens `/api/avatar/ws` and this module relays:

- **browser to avatar:** only `{"type": "speak", "text": ...}` (cleaned and capped), `{"type": "stop"}` and an allowed
  `{"type": "language", ...}`. Everything else is dropped, so the kiosk can't switch the avatar to another mode.
- **avatar to browser:** the video and audio frames as they come, and the JSON frames the player needs
  (`session_state`, `synctalk`, `avatar_reply`, `avatar-speak-completed`, `avatar_error`, ...). The service's greeting
  and course or MCP frames are dropped.

The avatar service signs callers in with a WorkflowHub platform token. The app uses the same platform session as the
skin model gateway (a service account, see platform_auth.GatewayTokens), so the token stays on the server and never
appears in a log, an error message or the browser.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.config import get_settings
from app.integrations.platform_auth import gateway_tokens

log = logging.getLogger("skincare.avatar")

# Languages the avatar service can speak (its service/languages.py). The consultation runs in English today.
LANGUAGES = {"en", "hi", "mr", "te", "ml", "ne", "bho", "ar", "bg", "hr", "cs", "da", "nl", "et", "fi", "fr", "de",
             "el", "hu", "id", "it", "ja", "ko", "lv", "lt", "pl", "pt", "ro", "ru", "sk", "sl", "es", "sv", "tr", "uk",
             "vi"}
# Frames the browser's player understands. Anything else from the service (its greeting, course events, MCP routing)
# is not for this app.
PASS_TYPES = {"session_state", "synctalk", "avatar_reply", "avatar-speak-completed", "avatar_error", "status",
              "audio_control", "emotion"}
STOP_FRAME = json.dumps({"type": "stop", "action": "stop", "value": "stop"})

# Close codes sent to the browser. 45xx: don't retry without the person asking; 1011/1013: try again later.
CLOSE_NO_SESSION = 4503  # no platform session for the avatar service
CLOSE_REFUSED = 4502  # the service refused the platform account
CLOSE_UNAVAILABLE = 4504  # the avatar doesn't exist or isn't active
CLOSE_IDLE = 4408  # nothing said for avatar_idle_close_s: the browser reconnects when it next speaks
CLOSE_UPSTREAM = 1011  # the service dropped or couldn't be reached


class AvatarUnavailable(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def enabled() -> bool:
    return bool(get_settings().avatar_ws_url)


def _platform_user_id(token: str) -> str | None:
    """The `sub` claim of a platform token (the avatar service puts the user id in the socket path). Not verified
    here: the avatar service checks the token itself."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        sub = claims.get("sub")
        return str(sub) if sub not in (None, "") else None
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


def session_url(token: str, avatar_id: int, language: str) -> str:
    s = get_settings()
    uid = _platform_user_id(token)
    if uid is None:
        raise AvatarUnavailable(CLOSE_NO_SESSION, "The platform session has no user id.")
    q = urlencode({"token": token, "avatar_id": avatar_id, "language": language})
    return f"{s.avatar_ws_url.rstrip('/')}/{uid}/ws?{q}"


_MARKDOWN = re.compile(r"[*_`#>]+")
_SPACES = re.compile(r"\s+")


def speakable(text: str) -> str:
    """What the avatar says for a chat message: no markdown, bullets or links, one line, capped."""
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")  # [label](url) -> label
    t = re.sub(r"https?://\S+", "", t)
    t = t.replace("•", " ").replace("·", " ")
    t = _MARKDOWN.sub("", t)
    t = _SPACES.sub(" ", t).strip()
    cap = get_settings().avatar_max_speech_chars
    if len(t) > cap:
        cut = t[:cap]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = cut[: end + 1] if end > cap // 2 else cut
    return t


def closed_reason(close: int | None) -> tuple[int, str]:
    """The browser's close code and reason for a session the avatar service closed."""
    if close in (4401, 4403, 1008):
        return CLOSE_REFUSED, "The avatar service refused the clinic's platform account."
    if close in (4404, 4503):
        return CLOSE_UNAVAILABLE, "This avatar isn't available."
    return CLOSE_UPSTREAM, "The avatar service closed the session."


class ClientSide(Protocol):
    """The browser end (a Starlette WebSocket, or a fake in tests)."""

    async def receive_text(self) -> str: ...
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


Connector = Callable[[str], Awaitable[Any]]


async def _default_connect(url: str):
    s = get_settings()
    return await websockets.connect(url, max_size=None, open_timeout=s.avatar_connect_timeout_s,
                                    ping_interval=20, ping_timeout=20)


@dataclass
class Relay:
    client: ClientSide
    who: str
    avatar_id: int
    language: str
    connect: Connector = _default_connect

    def __post_init__(self) -> None:
        self.last_activity = time.monotonic()
        self.spoken = 0

    async def _open(self):
        """Connect to the avatar service in Direct mode. A refused platform token is renewed and tried once more."""
        for attempt in (0, 1):
            token = await gateway_tokens.get(force=attempt == 1)
            if not token:
                raise AvatarUnavailable(CLOSE_NO_SESSION, "The avatar needs a platform session: set the service "
                                        "account (SKIN_MODEL_AUTH_EMAIL / SKIN_MODEL_AUTH_PASSWORD).")
            url = session_url(token, self.avatar_id, self.language)
            try:
                up = await self.connect(url)
            except InvalidStatus as e:
                status = e.response.status_code
                if status in (401, 403) and attempt == 0:
                    continue
                raise AvatarUnavailable(CLOSE_REFUSED if status in (401, 403) else CLOSE_UPSTREAM,
                                        f"The avatar service answered {status}.") from None
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                raise AvatarUnavailable(CLOSE_UPSTREAM, f"Can't reach the avatar service ({type(e).__name__}).") from None
            # Direct mode: the avatar says exactly what it is sent. The stop cuts the greeting the service plays on
            # connect (tested on the live service: nothing is spoken until the first text).
            try:
                await up.send(json.dumps({"type": "language", "value": self.language}))
                await up.send(json.dumps({"type": "mode", "value": "direct"}))
                await up.send(STOP_FRAME)
            except ConnectionClosed:
                code, message = closed_reason(getattr(up, "close_code", None))
                raise AvatarUnavailable(code, message) from None
            return up
        raise AvatarUnavailable(CLOSE_REFUSED, "The avatar service refused the clinic's platform account.")

    async def _from_client(self, up) -> None:
        while True:
            raw = await self.client.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "speak":
                text = speakable(str(msg.get("text") or ""))
                if text:
                    self.last_activity = time.monotonic()
                    self.spoken += 1
                    await up.send(text)
            elif kind == "stop":
                self.last_activity = time.monotonic()
                await up.send(STOP_FRAME)
            elif kind == "language" and msg.get("value") in LANGUAGES:
                self.language = msg["value"]
                await up.send(json.dumps({"type": "language", "value": self.language}))
            # anything else (mode switches, raw queries, audio) is not relayed

    async def _from_avatar(self, up) -> None:
        async for m in up:
            if isinstance(m, (bytes, bytearray)):
                await self.client.send_bytes(bytes(m))
                continue
            try:
                j = json.loads(m)
            except ValueError:
                continue
            if not isinstance(j, dict) or j.get("type") not in PASS_TYPES:
                continue
            if j.get("type") == "avatar_error":
                log.warning("[avatar] service reported %s: %s", j.get("code"), str(j.get("message"))[:200])
            await self.client.send_text(m)
            if j.get("type") == "avatar-speak-completed":
                self.last_activity = time.monotonic()

    async def _idle_watch(self) -> None:
        limit = get_settings().avatar_idle_close_s
        while True:
            await asyncio.sleep(min(5.0, limit / 4))
            if time.monotonic() - self.last_activity > limit:
                return

    async def run(self) -> None:
        try:
            up = await self._open()
        except AvatarUnavailable as e:
            log.warning("[avatar] not started for %s: %s", self.who, e.message)
            await self.client.close(code=e.code, reason=e.message[:120])
            return
        log.info("[avatar] session opened for %s (avatar %s, %s)", self.who, self.avatar_id, self.language)
        tasks = {asyncio.create_task(self._from_client(up), name="client"),
                 asyncio.create_task(self._from_avatar(up), name="avatar"),
                 asyncio.create_task(self._idle_watch(), name="idle")}
        code, reason = 1000, ""
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            first = done.pop()
            name, exc = first.get_name(), first.exception()
            if name == "idle":
                code, reason = CLOSE_IDLE, "Idle"
            elif name == "avatar":
                close = getattr(up, "close_code", None)
                code, reason = closed_reason(close)
                log.info("[avatar] service closed the session for %s (code %s)", self.who, close)
            elif exc is not None and not isinstance(exc, ConnectionClosed):
                log.info("[avatar] browser side ended for %s (%s)", self.who, type(exc).__name__)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await up.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
            try:
                await self.client.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001 - the browser already left
                pass
            log.info("[avatar] session closed for %s after %d lines", self.who, self.spoken)


def status() -> dict | None:
    """For /api/health: is the avatar on, and which one (never the token)."""
    s = get_settings()
    if not s.avatar_ws_url:
        return None
    return {"service": s.avatar_ws_url, "avatar_id": s.avatar_id, "language": s.avatar_language,
            "mode": "direct (says the assistant's replies)"}
