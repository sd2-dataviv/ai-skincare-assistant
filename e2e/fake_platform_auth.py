"""STAND-IN for WorkflowHub's auth service, for e2e/sso.mjs only: GET /api/tokens/details answers for a few fixed
platform tokens, like the real service (X-Auth-Token header; 401 otherwise).

    backend/.venv/bin/python e2e/fake_platform_auth.py --port 5011
"""
import argparse

import uvicorn
from fastapi import FastAPI, Header, HTTPException

USERS = {
    "PT-doctor-aaaaaaaaaaaaaaaaaaaaaaaaaaaa": {"email": "doctor@clinic.local", "full_name": "Dr. Kavya Rao"},
    "PT-doctor-bbbbbbbbbbbbbbbbbbbbbbbbbbbb": {"email": "doctor@clinic.local", "full_name": "Dr. Kavya Rao"},
    "PT-admin-cccccccccccccccccccccccccccccc": {"email": "admin@clinic.local", "full_name": "Clinic Admin"},
    "PT-stranger-dddddddddddddddddddddddddd": {"email": "someone@dataviv.in", "full_name": "Some One"},
}
app = FastAPI()
calls = {"details": 0}


@app.get("/api/tokens/details")
def details(x_auth_token: str | None = Header(None)):
    calls["details"] += 1
    info = USERS.get(x_auth_token or "")
    if info is None:
        raise HTTPException(401, "Invalid Token!")
    return {"sub": "81", "type": "access", "valid_token": True, "is_approved": True, **info}


@app.get("/calls")
def get_calls():
    return calls


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5011)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")
