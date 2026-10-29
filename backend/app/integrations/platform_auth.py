"""WorkflowHub platform auth: verify a platform token (SSO), and keep a valid token for the model gateway.

The auth service (WFH_AUTH_SERVICE_URL, injected by WorkflowHub; default http://192.168.0.84:5001/api) offers:
  GET  /tokens/details   X-Auth-Token: <token>   -> who the token belongs to (200) or a refusal
  POST /tokens/handoff   X-Auth-Token: <token>   {"client": "web"} -> a new access + refresh token for that user
  POST /tokens/refresh   {"refresh_token"}                         -> a new access token
  POST /users/login      {"email", "password"}                     -> access + refresh token

Every call fails CLOSED: an error, a refusal, a timeout or an unreachable service gives None, never an identity.

Model gateway tokens last 15 minutes, so the gateway token is kept fresh here, from the first source available:
  1. a service account (SKIN_MODEL_AUTH_EMAIL / SKIN_MODEL_AUTH_PASSWORD): logs in, refreshes, logs in again if needed;
  2. the platform session of the last staff member who signed in through SSO (handed off for this app, then refreshed);
  3. a fixed token (SKIN_MODEL_TOKEN), e.g. for a quick test.
Tokens never appear in logs, error messages or API responses.
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger("skincare.sso")
log.setLevel(logging.INFO)
if not log.handlers:  # uvicorn leaves the root logger at WARNING; handshake lines must be visible
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    log.addHandler(_h)
    log.propagate = False

_transport: httpx.AsyncBaseTransport | None = None  # test hook


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = t


def _client() -> httpx.AsyncClient:
    # The auth service is on the clinic/platform network: don't route it through an HTTP proxy from the environment.
    return httpx.AsyncClient(base_url=get_settings().wfh_auth_service_url.rstrip("/"), timeout=8, transport=_transport,
                             trust_env=False)


def token_expiry(token: str) -> float | None:
    """The token's exp claim (read, not verified: the auth service and the gateway do the verifying)."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


async def verify_platform_token(token: str) -> dict | None:
    if not token:
        return None
    try:
        async with _client() as c:
            r = await c.get("/tokens/details", headers={"X-Auth-Token": token})
    except httpx.HTTPError as e:
        log.warning("[SSO] auth service unreachable: %s", type(e).__name__)
        return None
    if r.status_code != 200:
        # The body tells an expired token from one minted by another auth service; it is an error, not a credential.
        log.warning("[SSO] token rejected by auth service (%s): %s", r.status_code, refusal_reason(r))
        return None
    try:
        info = r.json()
    except ValueError:
        return None
    if not isinstance(info, dict) or info.get("valid_token") is False:
        return None
    return info


@dataclass
class TokenPair:
    access: str
    refresh: str | None


last_refusal: str | None = None  # why the platform last refused a sign-in, for health and the avatar's message


def refusal_reason(r: httpx.Response) -> str:
    """The platform's reason code only (e.g. DEVICE_LIMIT_REACHED): never its full answer, which can list the
    account's devices and addresses."""
    try:
        d = r.json().get("detail")
    except (ValueError, AttributeError):
        return str(r.status_code)
    if isinstance(d, dict):
        return str(d.get("code") or r.status_code)
    return str(d)[:80] if d else str(r.status_code)


async def _pair(r: httpx.Response, what: str) -> TokenPair | None:
    global last_refusal
    if r.status_code not in (200, 201, 202):
        reason = refusal_reason(r)
        last_refusal = f"{what} refused ({r.status_code} {reason})"
        if reason == "DEVICE_LIMIT_REACHED":
            last_refusal += ": the account is signed in on its maximum number of devices; sign out of one"
        log.warning("[gateway token] %s", last_refusal)
        return None
    if what == "service account login":
        last_refusal = None
    try:
        d = r.json()
        return TokenPair(d["access_token"], d.get("refresh_token"))
    except (ValueError, KeyError, TypeError):
        return None


async def handoff(platform_token: str) -> TokenPair | None:
    try:
        async with _client() as c:
            return await _pair(await c.post("/tokens/handoff", json={"client": "web"},
                                            headers={"X-Auth-Token": platform_token}), "handoff")
    except httpx.HTTPError as e:
        log.warning("[gateway token] handoff failed: %s", type(e).__name__)
        return None


async def login(email: str, password: str) -> TokenPair | None:
    try:
        async with _client() as c:
            return await _pair(await c.post("/users/login", json={"email": email, "password": password}),
                               "service account login")
    except httpx.HTTPError as e:
        log.warning("[gateway token] service account login failed: %s", type(e).__name__)
        return None


class PlatformUnreachable(Exception):
    """The auth service could not be reached (as opposed to refusing the credentials)."""


async def password_login(email: str, password: str) -> TokenPair | None:
    """A person's platform email + password (the login form). None = refused; PlatformUnreachable = no answer."""
    try:
        async with _client() as c:
            r = await c.post("/users/login", json={"email": email, "password": password})
    except httpx.HTTPError as e:
        log.warning("[login] auth service unreachable: %s", type(e).__name__)
        raise PlatformUnreachable() from e
    if r.status_code not in (200, 201, 202):
        log.info("[login] platform refused the sign-in (%s)", r.status_code)  # never the email or password
        return None
    try:
        d = r.json()
        return TokenPair(d["access_token"], d.get("refresh_token")) if d.get("access_token") else None
    except (ValueError, KeyError, TypeError):
        return None


async def refresh(refresh_token: str) -> str | None:
    try:
        async with _client() as c:
            pair = await _pair(await c.post("/tokens/refresh", json={"refresh_token": refresh_token}), "refresh")
            return pair.access if pair else None
    except httpx.HTTPError as e:
        log.warning("[gateway token] refresh failed: %s", type(e).__name__)
        return None


class GatewayTokens:
    """A valid token for the model gateway, renewed before it expires."""

    MARGIN_S = 60  # renew this long before expiry

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.access: str | None = None
        self.refresh_token: str | None = None
        self.source: str | None = None  # "service account" | "platform session of <name>"
        self.renewed_at: float | None = None

    def reset(self) -> None:
        global last_refusal
        last_refusal = None
        self.access = self.refresh_token = self.source = None
        self.renewed_at = None

    @staticmethod
    def _service_account() -> tuple[str, str] | None:
        s = get_settings()
        return (s.skin_model_auth_email, s.skin_model_auth_password) if s.skin_model_auth_email and s.skin_model_auth_password else None

    def _fresh(self) -> bool:
        if not self.access:
            return False
        exp = token_expiry(self.access)
        return exp is None or exp - time.time() > self.MARGIN_S

    def _set(self, access: str, refresh_token: str | None, source: str) -> None:
        self.access, self.source, self.renewed_at = access, source, time.time()
        if refresh_token:
            self.refresh_token = refresh_token

    async def adopt_sso(self, platform_token: str, who: str) -> bool:
        """After an SSO sign-in: get this app its own refreshable session for the user (unless a service account is
        set up, which always wins)."""
        s = get_settings()
        if not s.skin_model_token_from_sso or self._service_account() or not (s.skin_diagnosis_ws_url or s.skin_post_treatment_ws_url):
            return False
        pair = await handoff(platform_token)
        if pair is None:
            # The platform allows the handoff for admins only. Use the person's own token instead: it lasts 15 minutes,
            # and the web app sends the current one again while it is open (POST /api/auth/platform-token).
            return await self.adopt_access(platform_token, who)
        async with self._lock:
            self._set(pair.access, pair.refresh, f"platform session of {who}")
            if not pair.refresh:
                self.refresh_token = None
        log.info("[gateway token] using the platform session of %s for the skin models", who)
        return True

    async def adopt_access(self, platform_token: str, who: str) -> bool:
        """A platform access token without a refresh token (from the browser). Never replaces a service account or a
        session that can renew itself; otherwise the token that lasts longest wins."""
        s = get_settings()
        if not s.skin_model_token_from_sso or self._service_account() or not (s.skin_diagnosis_ws_url or s.skin_post_treatment_ws_url):
            return False
        exp = token_expiry(platform_token)
        if exp is not None and exp - time.time() <= self.MARGIN_S:
            return False
        async with self._lock:
            if self.refresh_token and self._fresh():
                return False
            current = token_expiry(self.access) if self.access else None
            if self.access and current and exp and current >= exp and self._fresh():
                return False
            self.refresh_token = None
            self._set(platform_token, None, f"platform session of {who} (renewed by the open app)")
        log.info("[gateway token] using the platform session of %s for the skin models (no refresh token)", who)
        return True

    async def adopt_pair(self, pair: TokenPair, who: str) -> bool:
        """After a platform email + password sign-in: that session (with its refresh token) serves the models too."""
        s = get_settings()
        if not s.skin_model_token_from_sso or self._service_account() or not (s.skin_diagnosis_ws_url or s.skin_post_treatment_ws_url):
            return False
        async with self._lock:
            self._set(pair.access, pair.refresh, f"platform session of {who}")
            if not pair.refresh:
                self.refresh_token = None
        log.info("[gateway token] using the platform session of %s for the skin models", who)
        return True

    async def get(self, force: bool = False) -> str | None:
        async with self._lock:
            if not force and self._fresh():
                return self.access
            if self.refresh_token:
                new = await refresh(self.refresh_token)
                if new:
                    self._set(new, None, self.source or "platform session")
                    return new
                self.refresh_token = None  # expired or revoked: fall through
            acct = self._service_account()
            if acct:
                pair = await login(*acct)
                if pair:
                    self._set(pair.access, pair.refresh, "service account")
                    return pair.access
            if self.access and not force and self._fresh():
                return self.access
            fixed = get_settings().skin_model_token
            if fixed:
                self.access, self.source = None, "fixed token (SKIN_MODEL_TOKEN)"
                return fixed
            self.access = None
            return None

    def status(self) -> dict:
        """For /api/health: where the gateway token comes from, never the token."""
        exp = token_expiry(self.access) if self.access else None
        source = self.source
        if source is None:
            if self._service_account():
                source = "service account (signs in on first use)"
            elif get_settings().skin_model_token:
                source = "fixed token (SKIN_MODEL_TOKEN)"
            else:
                source = "none yet: sign in through WorkflowHub, or set a service account"
        out = {"source": source, "expires_in_s": int(exp - time.time()) if exp else None,
               "refreshable": bool(self.refresh_token or self._service_account())}
        if last_refusal and not self.access:
            out["problem"] = last_refusal
        return out


gateway_tokens = GatewayTokens()
