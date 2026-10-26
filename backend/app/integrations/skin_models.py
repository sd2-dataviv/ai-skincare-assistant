"""The clinic's skin model services, reached over WebSocket.

Protocol (both services): send the parameters as a JSON text message, then the photo as bytes. The service answers
with an image (JPEG bytes): the diagnosis service draws what it found on the face; the post-treatment service paints
the found spots out. Services updated for this app also send, right after the image, a JSON text message
`{"type": "predictions", "face_detected", "image_size", "detections": [{class, score, bbox, area_px}], ...}` when the
parameters carry `"return_predictions": true`. Older services ignore that key and send only the image; we then keep
the image and treat the findings as unknown.

A service that can't be reached is skipped for `skin_model_retry_after_s`, so a switched-off model server costs one
quick failed connection, not a wait on every photo.
"""
import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass, field

from app.config import get_settings

log = logging.getLogger(__name__)

# Model class -> the app's concern. Scars are reported to the doctor but are not one of the app's scored concerns.
CLASS_TO_CONCERN = {"acne": "acne", "pigmentation": "pigmentation", "dark circles": "dark_circles"}
EXTRA_CLASSES = {"scars": "scars"}


class ModelUnavailable(Exception):
    """The service could not be reached or did not answer in time."""


@dataclass
class ModelReply:
    image: bytes
    details: dict | None  # None: the service sent only the image (no findings)
    seconds: float
    content_type: str = "image/jpeg"


@dataclass
class ServiceConfig:
    name: str
    url: str
    params_text: str
    timeout_s: float
    _down_until: float = field(default=0.0, repr=False)

    def params(self) -> dict:
        try:
            p = json.loads(self.params_text)
        except ValueError as e:
            raise ValueError(f"{self.name}: parameters are not valid JSON ({e})") from e
        if not isinstance(p, dict):
            raise ValueError(f"{self.name}: parameters must be a JSON object")
        return p

    def inner_params(self) -> dict:
        p = self.params()
        return p["parameters"] if isinstance(p.get("parameters"), dict) else p

    def classes(self) -> list[str]:
        return list(self.inner_params().get("classes") or [])


def with_return_predictions(params: dict) -> dict:
    """Ask for the findings, inside the "parameters" wrapper when the parameters sent use one."""
    p = copy.deepcopy(params)
    target = p["parameters"] if isinstance(p.get("parameters"), dict) else p
    target["return_predictions"] = True
    return p


def _content_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def wants_token(url: str) -> bool:
    """Gateway addresses ask for a token (a token= parameter or a {token} placeholder); direct services don't."""
    from urllib.parse import parse_qsl, urlsplit

    return "{token}" in url or any(k == "token" for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))


def resolve_url(url: str, token: str | None = None) -> str:
    """Put the gateway token into a {token} placeholder or the token= parameter (whatever placeholder value it had).
    Only addresses that ask for a token get one, so a token is never sent to a service that didn't ask for it."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    tok = token if token is not None else get_settings().skin_model_token
    if not tok or not wants_token(url):
        return url
    if "{token}" in url:
        return url.replace("{token}", tok)
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "token"]
    return urlunsplit(parts._replace(query=urlencode([("token", tok)] + q)))


def _redact(text: str, *tokens: str | None) -> str:
    for tok in (*tokens, get_settings().skin_model_token):
        if tok:
            text = text.replace(tok, "<token>")
    return text


async def _connect(svc: "ServiceConfig"):
    """Open the WebSocket, with a gateway token when the address asks for one. A 401/403 gets one retry with a renewed
    token (it may have expired a moment ago). Returns (connection, token used)."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus, WebSocketException

    from app.integrations.platform_auth import gateway_tokens

    s = get_settings()
    tok = await gateway_tokens.get() if wants_token(svc.url) else None
    for attempt in (1, 2):
        try:
            async with asyncio.timeout(s.skin_model_connect_timeout_s):
                # proxy=None: the model servers are on the clinic network; never route them through an HTTP proxy.
                ws = await connect(resolve_url(svc.url, tok or ""), max_size=64 * 2**20,
                                   open_timeout=s.skin_model_connect_timeout_s, proxy=None, ping_interval=None)
                return ws, tok
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403) and wants_token(svc.url):
                if attempt == 1:
                    renewed = await gateway_tokens.get(force=True)
                    if renewed and renewed != tok:
                        tok = renewed
                        continue
                # A token problem, not a server problem: don't skip the server for the next call.
                why = ("no platform session for the model gateway yet: open the app from WorkflowHub, or set "
                       "SKIN_MODEL_AUTH_EMAIL / SKIN_MODEL_AUTH_PASSWORD" if not tok
                       else "the gateway refused the platform session and it could not be renewed")
                raise ModelUnavailable(f"{svc.name} refused the connection: HTTP {code} ({why}).") from e
            svc._down_until = time.monotonic() + s.skin_model_retry_after_s
            raise ModelUnavailable(f"{svc.name} refused the connection: HTTP {code}.") from e
        except (OSError, TimeoutError, WebSocketException) as e:
            svc._down_until = time.monotonic() + s.skin_model_retry_after_s
            raise ModelUnavailable(_redact(f"{svc.name} is not reachable ({type(e).__name__}: {e}).", tok)) from e
    raise ModelUnavailable(f"{svc.name} refused the connection.")


def _closed_message(svc: "ServiceConfig", e: Exception, tok: str | None) -> str:
    """The model gateway closes with 44xx codes when it won't run the model; say what to do about it."""
    rcvd = getattr(e, "rcvd", None)
    code, reason = getattr(rcvd, "code", None), (getattr(rcvd, "reason", "") or "")
    if code == 4401:
        return (f"{svc.name}: the model gateway needs a platform token ({reason or 'missing or expired'}). Open the "
                "app from WorkflowHub, or set SKIN_MODEL_AUTH_EMAIL / SKIN_MODEL_AUTH_PASSWORD.")
    if code in (4403, 4404):
        return (f"{svc.name}: the platform account in use may not run this model ({reason}). Share the model with it "
                "on WorkflowHub, or set SKIN_MODEL_AUTH_EMAIL / SKIN_MODEL_AUTH_PASSWORD to an account that can.")
    return f"{svc.name} closed the connection ({e})."


def _note(msg: str) -> str | None:
    """A text message from the service or gateway (notify=1): an error to report, or None for progress notes."""
    try:
        d = json.loads(msg)
    except ValueError:
        return msg[:200] if "error" in msg.lower() else None
    if not isinstance(d, dict):
        return None
    if d.get("error") or str(d.get("status", "")).lower() in ("error", "failed", "fail"):
        return str(d.get("error") or d.get("message") or d.get("detail") or d)[:200]
    return None


async def call_service(svc: ServiceConfig, image: bytes) -> ModelReply:
    """One round trip: parameters, image -> image (+ findings if the service sends them)."""
    from websockets.exceptions import WebSocketException

    s = get_settings()
    if time.monotonic() < svc._down_until:
        raise ModelUnavailable(f"{svc.name} was unreachable a moment ago; trying again shortly.")
    start = time.monotonic()
    ws, tok = await _connect(svc)
    notes: list[str] = []
    try:
        # Sent unwrapped by default: the deployed post-treatment service fails on {"parameters": ...} (it reads the
        # keys at the top level) and echoes the photo back; every version accepts the bare form.
        sent = svc.inner_params() if s.skin_model_unwrap_params else svc.params()
        await ws.send(json.dumps(with_return_predictions(sent)))
        await ws.send(image)
        try:
            async with asyncio.timeout(svc.timeout_s):
                first = await ws.recv()
                while isinstance(first, str):  # progress notes (notify=1) may come before the image
                    err = _note(first)
                    if err:
                        raise ModelUnavailable(_redact(f"{svc.name} reported an error: {err}", tok))
                    notes.append(first[:200])
                    first = await ws.recv()
        except TimeoutError as e:
            last = f" Last message: {notes[-1]!r}." if notes else ""
            raise ModelUnavailable(_redact(f"{svc.name} did not answer within {svc.timeout_s:.0f} s.{last}", tok)) from e
        if first == image:  # the services send the photo back untouched when processing it failed
            raise ModelUnavailable(f"{svc.name} sent the photo back unchanged, which means it failed to process it "
                                   "(check its parameters and its log).")
        details = None
        try:  # the findings follow the image; skip any progress notes in between
            async with asyncio.timeout(s.skin_model_details_wait_s):
                while details is None:
                    msg = await ws.recv()
                    if isinstance(msg, str):
                        try:
                            d = json.loads(msg)
                        except ValueError:
                            continue
                        if isinstance(d, dict) and d.get("type") == "predictions":
                            details = d
        except TimeoutError:
            pass  # an older service: image only
        return ModelReply(first, details, round(time.monotonic() - start, 2), _content_type(first))
    except WebSocketException as e:
        raise ModelUnavailable(_redact(_closed_message(svc, e, tok), tok)) from e
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def diagnosis_service() -> ServiceConfig | None:
    s = get_settings()
    return _service("skin diagnosis model", s.skin_diagnosis_ws_url, s.skin_diagnosis_params, s.skin_diagnosis_timeout_s)


def post_treatment_service() -> ServiceConfig | None:
    s = get_settings()
    return _service("skin post-treatment model", s.skin_post_treatment_ws_url, s.skin_post_treatment_params,
                    s.skin_post_treatment_timeout_s)


_services: dict[tuple, ServiceConfig] = {}


def _service(name: str, url: str | None, params: str, timeout: float) -> ServiceConfig | None:
    if not url:
        return None
    key = (name, url, params, timeout)
    if key not in _services:  # keep one object per service so the "down" timer is shared
        _services[key] = ServiceConfig(name, url, params, timeout)
    return _services[key]


def reset_services() -> None:
    """Test hook: forget down timers."""
    _services.clear()


# ── Scores from findings ─────────────────────────────────────────────────────
# Scores run 0-100 like the rest of the app (lower is better; <25 minimal, <45 mild, <70 moderate, else severe).
# These weights are a first calibration from lesion count, confidence and covered area; the clinic's doctors should
# check them against their own grading on real cases (see docs/07-skin-models.md).
def _clip(v: float) -> float:
    return round(max(3.0, min(95.0, v)), 1)


def _area_fraction(d: dict, image_area: float) -> float:
    area = d.get("area_px")
    if area is None:  # no mask: assume the lesion fills ~60% of its box
        x1, y1, x2, y2 = (d.get("bbox") or [0, 0, 0, 0])[:4]
        area = max(0, x2 - x1) * max(0, y2 - y1) * 0.6
    return float(area) / image_area if image_area else 0.0


def concern_score(concern: str, dets: list[dict], image_area: float) -> float:
    w = sum(float(d.get("score", 0)) for d in dets)  # confidence-weighted count
    a = sum(_area_fraction(d, image_area) for d in dets)
    if concern == "acne":
        return _clip(6 + 7 * w + 250 * a)
    if concern == "pigmentation":
        return _clip(6 + 4 * w + 900 * a)
    if concern == "dark_circles":
        if not dets:
            return _clip(8)
        mean = w / len(dets)
        return _clip(20 + 30 * mean * min(len(dets), 2) / 2 + 800 * a)
    return _clip(6 + 6 * w + 400 * a)


def summarise(per_pose: dict[str, dict], classes: list[str]) -> dict:
    """Combine the findings from the photos (front/left/right). The same spot can show in two photos, so each
    concern takes its worst photo rather than adding them up. Only poses where the model found a face count."""
    usable = {pose: d for pose, d in per_pose.items() if d and d.get("face_detected") and d.get("image_size")}
    scored: dict[str, dict] = {}
    extras: dict[str, dict] = {}
    for cls in classes:
        concern = CLASS_TO_CONCERN.get(cls) or EXTRA_CLASSES.get(cls)
        if concern is None or not usable:
            continue
        best = None
        for pose, d in usable.items():
            w, h = d["image_size"][:2]
            dets = [x for x in d.get("detections") or [] if x.get("class") == cls]
            score = concern_score(concern, dets, float(w * h))
            area_pct = round(100 * sum(_area_fraction(x, float(w * h)) for x in dets), 2)
            entry = {"score": score, "count": len(dets), "area_pct": area_pct, "pose": pose,
                     "mean_confidence": round(sum(float(x.get("score", 0)) for x in dets) / len(dets), 2) if dets else None}
            if best is None or entry["score"] > best["score"]:
                best = entry
        (scored if concern in CLASS_TO_CONCERN.values() else extras)[concern] = best
    return {"scores": scored, "extras": extras, "poses_used": sorted(usable)}
