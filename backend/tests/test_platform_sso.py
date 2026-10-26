"""WorkflowHub platform SSO and the model gateway's platform session, against a fake auth service that answers like
the real one (GET /tokens/details, POST /tokens/handoff, /tokens/refresh, /users/login)."""
import base64
import json
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import update
from websockets.asyncio.server import serve

from app.config import get_settings
from app.db import sessionmaker
from app.integrations import platform_auth as pa
from app.integrations import skin_models as sm
from app.integrations.platform_auth import gateway_tokens
from app.models import User
from tests.test_skin_models import jpeg


def jwt(sub: str, ttl: float, n: int = 0) -> str:
    """Shaped like the platform's tokens (header.payload.signature) with a real exp claim; not signed."""
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc({'sub': sub, 'n': n, 'exp': int(time.time() + ttl)})}.sig{n}"


class FakeAuth:
    def __init__(self):
        self.users = {}  # platform token -> details
        self.calls: list[str] = []
        self.refresh_ok = True
        self.handoff_admin_only = False
        self.n = 0
        self.issued: list[str] = []  # access tokens handed out, oldest first
        self.accounts = {"models@dataviv.in": ("pw", "Model Service")}  # platform email -> (password, name)
        app = FastAPI()

        @app.get("/api/tokens/details")
        def details(x_auth_token: str | None = Header(None)):
            self.calls.append("details")
            info = self.users.get(x_auth_token)
            if info is None:
                raise HTTPException(401, "Token is invalid or expired")
            return {"sub": "81", "type": "access", "valid_token": True, "is_approved": True, **info}

        @app.post("/api/tokens/handoff", status_code=201)
        def handoff(body: dict, x_auth_token: str | None = Header(None)):
            self.calls.append(f"handoff:{body.get('client')}")
            if x_auth_token not in self.users:
                raise HTTPException(401, "Token is invalid or expired")
            if self.handoff_admin_only and not self.users[x_auth_token].get("admin"):
                raise HTTPException(403, "Only admins can switch between the apps.")  # the real service's answer
            return self._pair()

        @app.post("/api/tokens/refresh", status_code=201)
        def refresh(body: dict):
            self.calls.append("refresh")
            if not self.refresh_ok or not str(body.get("refresh_token", "")).startswith("r-"):
                raise HTTPException(401, "Refresh token expired")
            self.n += 1
            self.issued.append(jwt("81", 900, self.n))
            return {"access_token": self.issued[-1], "token_type": "bearer"}

        @app.post("/api/users/login", status_code=202)  # the real service answers 202
        def login(body: dict):
            self.calls.append("login")
            acct = self.accounts.get(str(body.get("email", "")).lower())
            if acct is None or acct[0] != body.get("password"):
                raise HTTPException(401, "Incorrect email or password")
            pair = self._pair()
            self.users[pair["access_token"]] = {"email": body["email"].lower(), "full_name": acct[1]}
            return pair

        self.app = app

    def _pair(self) -> dict:
        self.n += 1
        self.issued.append(jwt("81", 900, self.n))
        return {"access_token": self.issued[-1], "refresh_token": f"r-{self.n}", "token_type": "bearer",
                "user_info": {}}


@pytest.fixture
def auth(monkeypatch):
    fake = FakeAuth()
    pa.set_transport(httpx.ASGITransport(app=fake.app))
    s = get_settings()
    for k, v in (("wfh_auth_mode", "platform"), ("wfh_default_role", None), ("skin_model_auth_email", None),
                 ("skin_model_auth_password", None), ("skin_model_token", None), ("skin_model_token_from_sso", True)):
        monkeypatch.setattr(s, k, v)
    yield fake
    pa.set_transport(None)
    gateway_tokens.reset()


async def sso(client, token: str):
    return await client.post("/api/auth/platform-session", json={"token": token})


# ── Signing staff in ─────────────────────────────────────────────────────────
async def test_platform_user_with_a_clinic_account_is_signed_in(client, auth):
    auth.users["PT-doctor-xxxxxxxx"] = {"email": "Doctor@Clinic.local", "full_name": "Dr. Kavya Rao"}
    r = await sso(client, "PT-doctor-xxxxxxxx")
    assert r.status_code == 200, r.text
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["role"] == "doctor" and me.json()["email"] == "doctor@clinic.local"
    assert (await client.get("/api/auth/platform-config")).json() == {"sso_enabled": True, "auth_mode": "platform"}


async def test_refusals_fail_closed(client, auth, monkeypatch):
    assert (await sso(client, "forged-token-1234")).status_code == 401
    auth.users["PT-noemail-xxxxxx"] = {"email": "", "full_name": "Nobody"}
    assert (await sso(client, "PT-noemail-xxxxxx")).status_code == 409

    def down(request):
        raise httpx.ConnectError("auth service down")

    pa.set_transport(httpx.MockTransport(down))
    auth.users["PT-doctor-xxxxxxxx"] = {"email": "doctor@clinic.local", "full_name": "Dr. Kavya Rao"}
    assert (await sso(client, "PT-doctor-xxxxxxxx")).status_code == 401  # an outage never mints a session

    monkeypatch.setattr(get_settings(), "wfh_auth_mode", "none")
    assert (await sso(client, "PT-doctor-xxxxxxxx")).status_code == 404


async def test_platform_users_without_a_clinic_account(client, auth, monkeypatch):
    auth.users["PT-stranger-xxxxx"] = {"email": "someone@dataviv.in", "full_name": "Some One", "admin": True}
    r = await sso(client, "PT-stranger-xxxxx")
    assert r.status_code == 403 and "Ask a clinic admin" in r.json()["detail"]  # platform admin is not clinic staff

    monkeypatch.setattr(get_settings(), "wfh_default_role", "doctor")  # never created this way
    assert (await sso(client, "PT-stranger-xxxxx")).status_code == 403

    monkeypatch.setattr(get_settings(), "wfh_default_role", "operator")
    r = await sso(client, "PT-stranger-xxxxx")
    assert r.status_code == 200 and r.json()["user"]["role"] == "operator"
    # the new account has no usable password: the login form can't get in
    r = await client.post("/api/auth/login", json={"email": "someone@dataviv.in", "password": ""})
    assert r.status_code in (401, 422)
    async with sessionmaker()() as db:
        await db.execute(update(User).where(User.email == "someone@dataviv.in").values(is_active=False))
        await db.commit()
    assert (await sso(client, "PT-stranger-xxxxx")).status_code == 403  # a disabled clinic account wins


# ── The model gateway's platform session ─────────────────────────────────────
class Gateway:
    """A model gateway that accepts the tokens `accept` allows (by default none), and records the ones it saw."""

    def __init__(self):
        self.accept = lambda tok: False
        self.seen: list[str] = []

    async def check(self, connection, request):
        from urllib.parse import parse_qs, urlsplit

        tok = parse_qs(urlsplit(request.path).query).get("token", [""])[0]
        self.seen.append(tok)
        if not self.accept(tok):
            return connection.respond(403, "invalid token\n")
        return None

    async def handler(self, ws):
        async for msg in ws:
            if isinstance(msg, bytes):
                await ws.send(jpeg((90, 110, 150)))


@pytest.fixture
async def gateway(monkeypatch):
    gw = Gateway()
    srv = await serve(gw.handler, "127.0.0.1", 0, process_request=gw.check).__aenter__()
    port = srv.sockets[0].getsockname()[1]
    s = get_settings()
    monkeypatch.setattr(s, "skin_diagnosis_ws_url", f"ws://127.0.0.1:{port}/ws/models/187/ws/cv/1001/image/image?token=&notify=1")
    monkeypatch.setattr(s, "skin_model_details_wait_s", 0.2)
    sm.reset_services()
    gw.svc = sm.diagnosis_service()
    yield gw
    srv.close()
    await srv.wait_closed()
    sm.reset_services()


async def test_staff_sign_in_gives_the_models_a_session_that_is_renewed(client, auth, gateway):
    svc = gateway.svc
    gateway.accept = lambda tok: tok in auth.issued
    with pytest.raises(sm.ModelUnavailable, match="open the app from WorkflowHub"):
        await sm.call_service(svc, jpeg())  # nobody signed in yet, no service account

    auth.users["PT-operator-xxxxxx"] = {"email": "operator@clinic.local", "full_name": "Front Desk Kiosk"}
    assert (await sso(client, "PT-operator-xxxxxx")).status_code == 200
    assert "handoff:web" in auth.calls
    first = gateway_tokens.access
    assert first == auth.issued[-1]  # the app's own handed-over session, not the browser's token
    await sm.call_service(svc, jpeg())
    assert gateway.seen[-1] == first

    # Nearly expired: renewed with the refresh token before the next call
    gateway_tokens.access = jwt("81", 30, 99)
    await sm.call_service(svc, jpeg())
    assert "refresh" in auth.calls and gateway.seen[-1] == auth.issued[-1] != first

    status = (await client.get("/api/health")).json()["skin_models"]["gateway_token"]
    assert status["source"] == "platform session of Front Desk Kiosk" and status["refreshable"]
    assert status["expires_in_s"] > 800 and auth.issued[-1] not in json.dumps(status)


async def test_a_refused_token_is_renewed_and_retried_once(client, auth, gateway):
    auth.users["PT-operator-xxxxxx"] = {"email": "operator@clinic.local", "full_name": "Front Desk Kiosk"}
    await sso(client, "PT-operator-xxxxxx")
    revoked = gateway_tokens.access
    # The gateway no longer accepts the current token (revoked or expired early); a renewed one works.
    gateway.accept = lambda tok: tok in auth.issued and tok != revoked
    await sm.call_service(gateway.svc, jpeg())
    assert gateway.seen[-2:] == [revoked, auth.issued[-1]] and "refresh" in auth.calls

    # Refresh refused too: a clear message, no token in it, and the server isn't treated as down
    auth.refresh_ok = False
    gateway.accept = lambda tok: False
    with pytest.raises(sm.ModelUnavailable, match="could not be renewed|open the app from WorkflowHub") as e:
        await sm.call_service(gateway.svc, jpeg())
    assert "eyJ" not in str(e.value) and gateway.svc._down_until == 0


async def test_a_service_account_is_preferred_over_staff_sessions(client, auth, gateway, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "skin_model_auth_email", "models@dataviv.in")
    monkeypatch.setattr(s, "skin_model_auth_password", "pw")
    gateway.accept = lambda tok: tok in auth.issued
    auth.users["PT-doctor-xxxxxxxx"] = {"email": "doctor@clinic.local", "full_name": "Dr. Kavya Rao"}
    assert (await sso(client, "PT-doctor-xxxxxxxx")).status_code == 200
    assert not any(c.startswith("handoff") for c in auth.calls)  # no session handed over needlessly
    await sm.call_service(gateway.svc, jpeg())
    assert "login" in auth.calls and gateway_tokens.source == "service account"

    monkeypatch.setattr(s, "skin_model_auth_password", "wrong")
    gateway_tokens.reset()
    with pytest.raises(sm.ModelUnavailable):
        await sm.call_service(gateway.svc, jpeg())


# ── The login form with platform email + password ────────────────────────────
async def login(client, email, password):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def test_login_form_takes_platform_email_and_password(client, auth, gateway):
    auth.accounts["doctor@clinic.local"] = ("platform-pw-1", "Dr. Kavya Rao")
    # clinic accounts keep working with their own password, without asking the platform
    r = await login(client, "doctor@clinic.local", "Demo@12345")
    assert r.status_code == 200 and "login" not in auth.calls

    r = await login(client, "Doctor@Clinic.local", "platform-pw-1")  # the WorkflowHub password
    assert r.status_code == 200 and r.json()["user"]["role"] == "doctor" and "login" in auth.calls
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["email"] == "doctor@clinic.local"
    # that platform session also serves the model gateway, refreshable, with no handoff needed
    assert gateway_tokens.access == auth.issued[-1] and gateway_tokens.refresh_token
    assert not any(c.startswith("handoff") for c in auth.calls)

    assert (await login(client, "doctor@clinic.local", "wrong")).status_code == 401


async def test_login_form_platform_refusals(client, auth, monkeypatch):
    stranger = f"stranger-{uuid.uuid4().hex[:8]}@dataviv.in"  # staff accounts persist across tests: a fresh email
    auth.accounts[stranger] = ("pw-2", "Some One")
    r = await login(client, stranger, "pw-2")  # right platform password, no clinic account
    assert r.status_code == 403 and "Ask a clinic admin" in r.json()["detail"]
    assert (await login(client, "nobody@dataviv.in", "x")).status_code == 401

    def down(request):
        raise httpx.ConnectError("auth service down")

    pa.set_transport(httpx.MockTransport(down))
    r = await login(client, stranger, "pw-2")
    assert r.status_code == 503 and "Clinic accounts still work" in r.json()["detail"]
    assert (await login(client, "operator@clinic.local", "Demo@12345")).status_code == 200  # local still fine

    monkeypatch.setattr(get_settings(), "wfh_auth_mode", "none")  # SSO off: the platform isn't asked at all
    assert (await login(client, stranger, "pw-2")).status_code == 401


# ── Regular platform users (handoff is admin-only) ───────────────────────────
async def test_regular_platform_user_becomes_an_operator_and_their_token_serves_the_models(client, auth, gateway, monkeypatch):
    monkeypatch.setattr(get_settings(), "wfh_default_role", "operator")  # the shipped default
    auth.handoff_admin_only = True
    first = jwt("70", 900, 500)
    auth.users[first] = {"email": "developer@dataviv.in", "full_name": "developer", "user_role": "user"}
    r = await sso(client, first)
    assert r.status_code == 200 and r.json()["user"]["role"] == "operator"
    assert "handoff:web" in auth.calls  # tried, refused (admin-only) ...
    assert gateway_tokens.access == first and not gateway_tokens.refresh_token  # ... so their own token is used
    gateway.accept = lambda tok: tok == first
    await sm.call_service(gateway.svc, jpeg())

    # The open app passes on the newer token WorkflowHub gave the browser; the models switch to it.
    op = {"Authorization": f"Bearer {r.json()['access_token']}"}
    newer = jwt("70", 900, 501)
    auth.users[newer] = auth.users[first]
    r = await client.post("/api/auth/platform-token", json={"token": newer}, headers=op)
    assert r.status_code == 200
    assert (r.json()["used_for_models"] and gateway_tokens.access == newer) or gateway_tokens.access == first
    assert (await client.post("/api/auth/platform-token", json={"token": "forged-token-123"}, headers=op)).status_code == 401
    assert (await client.post("/api/auth/platform-token", json={"token": newer})).status_code == 401  # staff only


async def test_a_refreshable_or_service_session_is_never_replaced_by_a_browser_token(client, auth, gateway, monkeypatch):
    auth.users["PT-doctor-xxxxxxxx"] = {"email": "doctor@clinic.local", "full_name": "Dr. Kavya Rao"}
    r = await sso(client, "PT-doctor-xxxxxxxx")  # handoff allowed here: a refreshable session
    kept = gateway_tokens.access
    later = jwt("70", 900, 777)
    auth.users[later] = {"email": "doctor@clinic.local", "full_name": "Dr. Kavya Rao"}
    r = await client.post("/api/auth/platform-token", json={"token": later},
                          headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert r.json() == {"used_for_models": False} and gateway_tokens.access == kept


async def test_gateway_refusing_the_model_is_explained(monkeypatch):
    async def no_access(ws):
        await ws.recv()
        await ws.close(4404, "no prediction model 187 you can use")

    srv = await serve(no_access, "127.0.0.1", 0).__aenter__()
    try:
        monkeypatch.setattr(get_settings(), "skin_model_token", "SOME.platform.token")
        svc = sm.ServiceConfig("skin diagnosis model", f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/x?token=&notify=1", "{}", 5)
        with pytest.raises(sm.ModelUnavailable) as e:
            await sm.call_service(svc, jpeg())
        msg = str(e.value)
        assert "may not run this model" in msg and "no prediction model 187" in msg and "SKIN_MODEL_AUTH_EMAIL" in msg
        assert "SOME.platform" not in msg
    finally:
        srv.close()
        await srv.wait_closed()
