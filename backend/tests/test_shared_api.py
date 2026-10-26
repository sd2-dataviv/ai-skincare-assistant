"""The API shared with other apps and AI agents: WorkflowHub forwards calls as the person asking, with a signed
X-Wfh-User header. Only the endpoints in node.yaml are open to it, each with its usual role check."""
import time
import uuid
from pathlib import Path

import jwt
import pytest
import yaml

from app.config import get_settings
from app.shared_api import SHARED

SECRET, AUD = "wfh-app-secret-for-tests-0123456789abcdef", "app:ai_skincare_assistant"


def wfh(email: str, name: str = "Someone", ttl: float = 300, secret: str = SECRET, aud: str = AUD, iss: str = "workflowhub"):
    now = int(time.time())
    return {"X-Wfh-User": jwt.encode({"sub": "81", "email": email, "name": name, "aud": aud, "iss": iss,
                                      "iat": now, "exp": now + int(ttl)}, secret, algorithm="HS256")}


@pytest.fixture
def api_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "wfh_app_api_secret", SECRET)
    monkeypatch.setattr(s, "wfh_app_api_audience", AUD)
    monkeypatch.setattr(s, "wfh_default_role", None)


def test_node_yaml_and_the_shared_list_match():
    node = Path(__file__).resolve().parents[2] / "node.yaml"
    if not node.exists():
        pytest.skip("node.yaml sits at the project root, outside the backend image")
    api = yaml.safe_load(node.read_text())["api"]
    declared = {(e["method"].upper(), api["base_path"] + e["path"]): e["key"] for e in api["endpoints"]}
    assert declared == SHARED


async def test_calls_as_the_person_asking(client, api_on):
    r = await client.get("/api/doctor/queue", headers=wfh("Doctor@Clinic.local", "Dr. Kavya Rao"))
    assert r.status_code == 200, r.text
    r = await client.get("/api/catalogue", headers=wfh("operator@clinic.local"))
    assert r.status_code == 200 and len(r.json()) > 5
    # the endpoint's own role check still applies: the clinic device can't see the doctor queue
    assert (await client.get("/api/doctor/queue", headers=wfh("operator@clinic.local"))).status_code == 403
    r = await client.get("/api/ot/reports", params={"from": "2026-10-01", "to": "2026-10-26"}, headers=wfh("admin@clinic.local"))
    assert r.status_code == 200


async def test_forged_expired_or_misdirected_headers_are_refused(client, api_on):
    assert (await client.get("/api/catalogue", headers=wfh("doctor@clinic.local", secret="guessed-secret-0123456789abcdef-0123"))).status_code == 401
    assert (await client.get("/api/catalogue", headers=wfh("doctor@clinic.local", ttl=-60))).status_code == 401
    assert (await client.get("/api/catalogue", headers=wfh("doctor@clinic.local", aud="app:another_app"))).status_code == 401
    assert (await client.get("/api/catalogue", headers=wfh("doctor@clinic.local", iss="someone-else"))).status_code == 401


async def test_only_shared_endpoints_and_clinic_staff(client, api_on):
    # not in node.yaml: refused even for an admin (WorkflowHub refuses these too; the app doesn't rely on it)
    r = await client.get("/api/admin/users", headers=wfh("admin@clinic.local"))
    assert r.status_code == 403 and "not shared" in r.json()["detail"]
    assert (await client.get("/api/ot/blocks", headers=wfh("admin@clinic.local"))).status_code == 403
    # a platform user without a clinic staff account
    r = await client.get("/api/catalogue", headers=wfh(f"stranger-{uuid.uuid4().hex[:8]}@dataviv.in"))
    assert r.status_code == 403 and "Ask a clinic admin" in r.json()["detail"]


async def test_sharing_is_off_until_workflowhub_provides_the_secret(client, monkeypatch, operator):
    monkeypatch.setattr(get_settings(), "wfh_app_api_secret", None)
    r = await client.get("/api/catalogue", headers=wfh("doctor@clinic.local"))
    assert r.status_code == 401 and "not enabled" in r.json()["detail"]
    assert (await client.get("/api/catalogue", headers=operator)).status_code == 200  # normal sign-in unaffected
