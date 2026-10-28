# AI Skincare Assistant

An AI assistant runs the skin consultation that a doctor does by hand today:
- identifies the client by OTP;
- asks the *Know your Skin* questions;
- guides three face photos;
- runs the analysis;
- drafts a plan from the clinic catalogue;
- hands everything to a doctor for one-tap approval.

The doctor stays responsible: every plan is approved by a registered practitioner, and the AI hands the visit over automatically when a safety rule fires.

Each visit is filed under one of the clinic's services: **Acne Scription, Pigmentation Scription, Age Scription, Skin Scription, Anti Aging** or **Medical Consultation**, picked from what the client says. A returning client's next session is its **Follow Up** (or **Medical Follow Up**). The services are set in `backend/app/knowledge/rules.json`.

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


## The clinic's skin models

Set `ANALYSIS_ENGINE=ws` and the two model addresses in `backend/.env`, then restart the API:
- The diagnosis model scores acne, pigmentation and dark circles, and marks them on the photos.
- The post-treatment model makes a preview of the face with those spots painted out.

The doctor sees the pictures in review. The client sees them only once the plan is approved, and the preview is always labelled as an illustration.

If a model server is down, the consultation carries on with estimates.

[docs/07-skin-models.md](docs/07-skin-models.md) covers the protocol, the changes made to the two service folders, who sees what, and a stand-in server for demos (`make stand-in-models`).

## The talking avatar

On the clinic device, the clinic's AI Avatar service (Meera, `wss://avatar.instaviv.in`) says the assistant's replies
with a face and voice. It runs in the service's Direct mode, so it says exactly what the assistant wrote; the app's
own assistant still decides every word. Set `AVATAR_WS_URL=wss://avatar.instaviv.in` (and the platform service
account used for the skin models). The browser reaches the avatar only through the API's relay
(`/api/avatar/ws`), so the platform token stays on the server. Without it, or when the avatar can't be reached,
the browser's own voice speaks as before. See [docs/09-avatar.md](docs/09-avatar.md).

## Signing in from WorkflowHub

Opened from WorkflowHub, the app signs clinic staff in automatically with their platform session (matched by email).
The login form also takes their WorkflowHub email and password. Platform users with no clinic account become
operators (they run consultations); doctors and admins sign in with their clinic accounts.

Ten endpoints are shared with other apps and AI agents (`node.yaml` `api:`). WorkflowHub calls them as the person
asking, and each keeps its usual role check.

The same platform session, renewed by the app, authorises the skin model gateway. A platform service account
(`SKIN_MODEL_AUTH_EMAIL` / `SKIN_MODEL_AUTH_PASSWORD`) is better for production: it works with nobody signed in.

See [docs/08-platform-sso.md](docs/08-platform-sso.md).

## Run with Docker, or upload to WorkflowHub

The folder is ready to zip and upload as an **application**, following WorkflowHub's "Building an Application" guide:
- `node.yaml` gives the permanent `key`, a one-line `description`, and the website's container port, 8080.
- `docker-compose.yml` runs the website (`web`: nginx with the React app, forwarding `/api/...`), the `api` and the
  `db`. Only the container port is listed, so the platform picks the host port.
- Data lives in named volumes, and the database has a health check.

Locally:

```
cp .env.example .env              # then fill in the values
docker compose up --build
docker compose port web 8080      # the address to open
```

Settings and secrets come from `.env`, which the `api` reads through `env_file`. It is never baked into an image, and
WorkflowHub never includes it when the code is downloaded. Values in `.env.example`:

| Value | What it does | In `.env.example` |
|---|---|---|
| `LLM_API_KEY` | Groq key; turns on the AI assistant | empty = scripted assistant |
| `JWT_SECRET` | signs staff and kiosk logins (32+ characters) | a placeholder: change it |
| `POSTGRES_PASSWORD` | database password (the database is never exposed) | a demo value |
| `PUBLIC_BASE_URL` | the app's public address, used in texts that link clients to `/my` | empty = relative links |
| `SEED_DEMO_USERS` | `1` creates doctor@ / admin@ / operator@clinic.local | `1` |
| `DEMO_PASSWORD` | password for those demo accounts | `Demo@12345` |
| `PHOTO_FACE_CHECK` | `false` accepts photos without a detectable face | `true` |

WorkflowHub injects its own `WFH_*` values (such as `WFH_AUTH_SERVICE_URL`); the compose file leaves them alone.

The live camera needs HTTPS (browsers block it on plain http other than localhost); photo upload works either way.
Build the zip without local files: no `backend/.env`, `.venv`, `node_modules`, `dist`, `var` or `e2e`. The root
`.env` is included only when you want the platform to use it.

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
