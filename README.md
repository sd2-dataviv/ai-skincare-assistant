# AI Skincare Assistant

An AI assistant runs the skin consultation that a doctor does by hand today:
- identifies the client by OTP;
- asks the *Know your Skin* questions;
- guides three face photos;
- runs the analysis;
- drafts a plan from the clinic catalogue;
- hands everything to a doctor for one-tap approval.

The doctor stays responsible: every plan is approved by a registered practitioner, and the AI hands the visit over automatically when a safety rule fires.

Afterwards, clients can chat about any past consultation on the clinic device or on their own phone (`/my`, mobile + OTP). The AI answers from the approved plan and hands over to the clinic team when a person is needed, and doctors and admins can message any client from the Messages tab.

**Stack:** FastAPI and SQLAlchemy 2 (async) with PostgreSQL 16 and Alembic. React 18, TypeScript and Vite. OpenCV for photo quality gates. Groq (or any OpenAI-compatible LLM) for the conversation, with a deterministic offline fallback.

## Quick start (local, no Docker)

You need Python 3.11+ with [uv](https://docs.astral.sh/uv/), Node 18+, and the PostgreSQL 16 server binaries (`postgresql-16`).

```bash
make setup        # local Postgres on :5433, Python venv, npm install, migrations, demo users
make api          # terminal 1 → http://127.0.0.1:8000/api/docs
make web          # terminal 2 → http://localhost:5173
```

Sign in with the demo accounts below. The password for all of them is `Demo@12345`.

| Account | Use |
|---|---|
| `operator@clinic.local` | The clinic device: consent, then the AI session |
| `doctor@clinic.local` | Doctor review queue (Dr. Kavya Rao, Reg. No. DMC/R/12345) |
| `admin@clinic.local` | User management (API) and access to the queue |

After the doctor approves, the AI books each clinic treatment in the plan into a suitable treatment room (OT): it offers the three earliest free times, and the client taps one, asks for other days, or books later. Doctors and admins (front desk) see all rooms on **Treatment rooms** (`/ot`); the clinic device doesn't, because the calendar shows other clients' names: a day calendar with bookings and blocks, a list of treatments still to book, and a form to block time. Three demo rooms are seeded (Laser Suite, Procedure Room, Facial Room; 10:00–19:00, Monday to Saturday).

Bookings also check the doctor (working hours, leave, not in two rooms at once) and the machine a treatment needs (four demo machines; one treatment at a time, plus cool-down). Times picked in the chat are **tentative** until staff confirm them on the calendar, which texts the client. Each booking has a pre-treatment checklist; staff check the client in, start the treatment (only when the checklist is complete), and finish it. Staff can book outside hours, on a holiday or when the doctor is off shift with a written reason. The **Machines, hours & holidays** tab sets these up (admins), and **Reports** shows room utilisation, turnover, on-time starts and no-shows, with a CSV export.

For a demo session, use mobile **98765 43210** (imported from the mock D365 as Priya Sharma) and OTP **123456**. Other sample numbers:
- **98111 22233**: Rohan Mehta.
- **99000 11122**: a 16-year-old, which shows the under-18 hand-over.
- Any other number creates a new client.

To use the real LLM, set `LLM_API_KEY` (Groq) in `backend/.env`; the default model is `openai/gpt-oss-120b` (`LLM_MODEL`). Without a key the scripted agent runs the same flow. A camera without a real face fails the face check; set `PHOTO_FACE_CHECK=false` for synthetic or demo cameras.

## Quick start on Windows

No `make` on Windows: use `scripts\windows\dev.ps1` in PowerShell (needs Docker Desktop, Python 3.12+, Node 20+).

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\dev.ps1 setup   # first time: DB container, backend\.venv-win, npm ci, migrate, seed
powershell -ExecutionPolicy Bypass -File .\scripts\windows\dev.ps1 start   # DB + API :8000 + web :5173 (logs in var\logs)
powershell -ExecutionPolicy Bypass -File .\scripts\windows\dev.ps1 stop    # stop API and web (DB container keeps running)
powershell -ExecutionPolicy Bypass -File .\scripts\windows\dev.ps1 test    # backend tests
```

PostgreSQL runs in Docker (container `skincare-db`, 127.0.0.1:5433, data in the `skincare-pgdata` volume), so `backend\.env` works unchanged. A `.venv` or `node_modules` copied from Linux won't run on Windows: the script uses `backend\.venv-win`, and `npm ci` reinstalls `node_modules`.

## Run with Docker, or upload to the Workflow Platform

The folder is ready to zip and upload as an **application** (`docker-compose.yml` + `node.yaml`; the screen people
open is the `web` part on port 8080). Locally: `docker compose up --build`, then open http://localhost:8080.

Every setting has a working default, so it starts with no configuration. Set these as environment values on the
platform (never put them in the zip):

| Value | What it does | Default |
|---|---|---|
| `LLM_API_KEY` | Groq key; turns on the AI assistant | empty = scripted assistant |
| `JWT_SECRET` | signs staff and kiosk logins (32+ characters) | a demo value, change it |
| `POSTGRES_PASSWORD` | database password (the database is never exposed) | a demo value |
| `PUBLIC_BASE_URL` | the app's public address, used in texts that link clients to `/my` | empty = texts say to sign in on the clinic's website |
| `SEED_DEMO_USERS` | `1` creates doctor@ / admin@ / operator@clinic.local | `1` |
| `DEMO_PASSWORD` | password for those demo accounts | `Demo@12345` |
| `PHOTO_FACE_CHECK` | `false` accepts photos without a detectable face | `true` |

The live camera needs HTTPS (browsers block it on plain http other than localhost); photo upload works either way.
Build the zip without local files: no `backend/.env`, `.venv`, `node_modules`, `dist` or `e2e`.

## Tests

```bash
make test          # 116 backend tests on a real Postgres (unit, API, full flows, escalation, LLM tool loop)
make typecheck     # frontend TypeScript
make e2e           # browser run: consultation → doctor approval → hand-over (needs `make api-demo` and `make web`)
node e2e/chats.mjs # browser run: follow-up chats on the clinic device, Messages tab and the client's phone (/my)
node e2e/ot.mjs    # browser run: tentative chat booking → confirm → checklist → start/finish; setup; reports (needs `make api-demo`)
```

## Project layout

```
docs/        SDLC documents: requirements → architecture → data model → API → test plan → deployment & security
backend/     FastAPI app (app/), Alembic migrations, tests
frontend/    React app: AI session, results, summary, doctor review
e2e/         Browser end-to-end run (playwright-core driving system Chrome)
scripts/     dev-db.sh: project-local PostgreSQL cluster
```

Start with `docs/01-requirements.md` for the requirement IDs and the open questions for legal and clinical sign-off.
