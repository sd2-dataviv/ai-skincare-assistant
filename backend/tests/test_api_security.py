"""Auth on every endpoint, no-store headers, admin user management, REST action endpoints."""
import pytest


async def test_login_and_me(client, operator):
    r = await client.get("/api/auth/me", headers=operator)
    assert r.json()["role"] == "operator"
    bad = await client.post("/api/auth/login", json={"email": "operator@clinic.local", "password": "wrong"})
    assert bad.status_code == 401


@pytest.mark.parametrize("method,path", [
    ("get", "/api/clients"), ("get", "/api/catalogue"), ("post", "/api/conversations"), ("get", "/api/doctor/queue"),
    ("get", "/api/visits/00000000-0000-0000-0000-000000000000"),
])
async def test_endpoints_require_auth(client, method, path):
    r = await getattr(client, method)(path)
    assert r.status_code == 401


async def test_no_store_header(client, operator):
    r = await client.get("/api/catalogue", headers=operator)
    assert r.headers["cache-control"] == "no-store"
    assert len(r.json()) >= 20


async def test_doctor_queue_forbidden_for_operator(client, operator):
    assert (await client.get("/api/doctor/queue", headers=operator)).status_code == 403


async def test_admin_creates_doctor_with_registration(client, admin):
    r = await client.post("/api/admin/users", json={"email": "dr.new@clinic.local", "full_name": "Dr New", "role": "doctor",
                                                   "password": "LongEnough#1"}, headers=admin)
    assert r.status_code == 422
    r = await client.post("/api/admin/users", json={"email": "dr.new@clinic.local", "full_name": "Dr New", "role": "doctor",
                                                   "password": "LongEnough#1", "registration_no": "MCI/9"}, headers=admin)
    assert r.status_code == 201


async def test_rest_actions_mirror_ai_actions(client, operator, doctor):
    from tests.conftest import face_like_jpeg

    r = await client.post("/api/clients/import", json={"mobile": "9811122233"}, headers=operator)
    assert r.json()["status"] == "imported"
    cid = r.json()["client"]["id"]
    v = await client.post(f"/api/clients/{cid}/visits", json={"concern_text": "fine lines around my eyes"},
                          headers={**operator, "Idempotency-Key": "visit-key-1"})
    v2 = await client.post(f"/api/clients/{cid}/visits", json={"concern_text": "fine lines around my eyes"},
                           headers={**operator, "Idempotency-Key": "visit-key-1"})
    assert v.json()["id"] == v2.json()["id"]  # exactly once
    vid = v.json()["id"]
    assert v.json()["programme"] == "anti_ageing"
    answers = {f"q{i:02d}": "x" for i in range(1, 19)}
    bad = await client.put(f"/api/visits/{vid}/assessment", json={"answers": answers}, headers=operator)
    assert bad.status_code == 422
    photo_ids = []
    for i, pose in enumerate(["front", "left", "right"]):
        p = await client.post(f"/api/visits/{vid}/photos", data={"pose": pose},
                              files={"file": ("a.jpg", face_like_jpeg(i + 3), "image/jpeg")}, headers=operator)
        assert p.status_code == 201 and p.json()["accepted"]
        photo_ids.append(p.json()["id"])
    img = await client.get(f"/api/photos/{photo_ids[0]}/file", headers=doctor)
    assert img.headers["content-type"] == "image/jpeg"
    a = await client.post(f"/api/photos/{photo_ids[0]}/analyses", headers=operator)
    assert a.status_code == 200 and "wrinkles" in a.json()["diagnosis"]
    assert (await client.get(f"/api/visits/{vid}/recommendations", headers=operator)).status_code == 403
    assert (await client.get(f"/api/visits/{vid}", headers=operator)).status_code == 403  # drafts are doctor-only
    rec = (await client.get(f"/api/visits/{vid}/recommendations", headers=doctor)).json()
    assert rec["items"]
    rx = await client.put(f"/api/visits/{vid}/prescription", json={"items": [
        {"catalogue_id": i["catalogue_id"]} for i in rec["items"]]}, headers=operator)
    assert rx.status_code == 200
    ap = await client.post(f"/api/visits/{vid}/approval", headers=operator)
    assert ap.json()["status"] == "pending"
    d = await client.post(f"/api/visits/{vid}/approval/decision", json={"decision": "approve"}, headers=doctor)
    assert d.status_code == 200
    share = await client.post(f"/api/visits/{vid}/summary/share", json={"channel": "link"}, headers=operator)
    assert share.json()["url"].startswith("http")
    detail = (await client.get(f"/api/visits/{vid}", headers=doctor)).json()
    assert {a["actor"] for a in detail["actions"]} == {"staff"}
