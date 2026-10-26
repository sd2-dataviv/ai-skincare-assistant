# 06 · Deployment, Security and Operations

## Environments

| | Development | Production |
|---|---|---|
| Database | Project-local Postgres on `127.0.0.1:5433` (`scripts/dev-db.sh`) | Managed Postgres 16 or the `db` service in `docker-compose.yml` |
| LLM | `scripted` if no key; Groq `openai/gpt-oss-120b` with `LLM_API_KEY` | Groq, or self-hosted Ollama/vLLM through `LLM_BASE_URL` |
| Adapters | All `mock` | Real analysis engine, D365 gateway, SMS/WhatsApp, S3-compatible storage |
| OTP | `mock` (code `123456`) | Real SMS. **The API refuses to start in production with `OTP_MODE=mock` or the default `JWT_SECRET`** |

## Deploying with Docker

```bash
cp backend/.env.example backend/.env    # set JWT_SECRET, LLM_API_KEY, ENVIRONMENT=production, OTP_MODE=sms …
export POSTGRES_PASSWORD='…'
ADMIN_EMAIL=admin@clinic.in ADMIN_PASSWORD='…' docker compose up -d --build
```

On start, the `api` container runs `alembic upgrade head`, then seeds or updates the catalogue, then starts uvicorn. Put TLS in front of `web` on port 8080, for example a load balancer or Caddy with `reverse_proxy web:80`. The camera and microphone only work over HTTPS (or localhost).

## Must-haves from spec §08

| Must-have | Implementation | Verified by |
|---|---|---|
| Consent before the session | `POST /conversations` returns 400 without both consents. Consent times are stored | `test_consent_required` and the e2e consent screen |
| Every AI action logged with inputs, result and reason | `ActionLayer.call()` writes `ai_actions`. Blocked and failed actions are logged too. Staff REST calls are logged as `actor=staff` | `test_full_consultation_happy_path`, `test_ai_cannot_skip_stages` |
| No prescription without doctor approval | Prescription state machine. `build_summary` and `share` refuse anything not approved. The draft is labelled as a draft | Summary returns 403 before approval (happy-path test) |
| Escalation rules in code | `services/escalation.py` runs after every action and on every user message. In `handed_over` mode the AI can't act | 6 escalation tests |
| HTTPS, auth on every endpoint, no secrets in the repo | JWT on every route. Secrets come from env. `backend/.env` is git-ignored. Security headers are set and CORS is restricted | `test_endpoints_require_auth` |
| No health data on the device, in logs or analytics | `Cache-Control: no-store`. The frontend keeps state in memory; photos are captured to an in-memory canvas and uploaded. Log redaction is on. There's no analytics SDK. Fonts are self-hosted | `test_log_redaction`, `test_no_store_header` |
| Exactly-once submission, limited retries | `client_message_id` unique key, `Idempotency-Key` store and state-machine guards. Integrations retry at most 3 times, the LLM at most 2 per call, and tool loops are capped at 6 steps. The OTP has attempt and send limits | `test_message_exactly_once`, idempotency assertions |

## Threats to watch in production

- **Prompt injection through the client's speech.** The LLM can only call stage-allowed tools. Every tool validates its input and only uses catalogue IDs. Escalation runs in code, so a manipulated model can't skip review.
- **Client phone sign-in.** `/api/client-auth/start` answers the same for unknown numbers, so it can't be used to list clients. Client tokens carry `role=client` and every staff route refuses them; client routes check ownership on every thread, visit and summary. Set `PUBLIC_BASE_URL` so the text a client gets when staff reply links to `/my`.
- **OTP abuse.** Limits are 3 sends per 10 minutes and 5 attempts per code. Put rate limiting at the edge too.
- **Photo storage.** Use encrypted object storage with private ACLs. Photo URLs are never public; the API streams them to authenticated staff only.
- **Retention.** Retention periods aren't defined yet (DPDP Act 2023). Add a scheduled purge once legal sets them.

## Operations

- **Health:** `GET /api/health` reports the LLM and analysis adapter in use.
- **Audit:** the `ai_actions` table is the audit trail. The doctor console shows it per visit.
- **Switching the LLM:** set `LLM_PROVIDER` to `groq`, `openai` or `ollama` (plus `LLM_API_KEY`, `LLM_MODEL` and `LLM_BASE_URL`). If the provider fails, the scripted agent answers that turn, so the visit never stalls.
- **Plugging in real services:** set `ANALYSIS_ENGINE=http` and `ANALYSIS_ENGINE_URL`, and `D365_MODE=http` and `D365_BASE_URL`. Expected payloads are in `app/integrations/*.py`.
