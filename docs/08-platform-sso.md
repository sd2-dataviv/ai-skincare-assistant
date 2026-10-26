# 08 · WorkflowHub platform SSO and the model gateway token

When the app is opened from WorkflowHub, clinic staff are signed in without the login form. The same platform session
gives the app the token the skin model gateway needs. This follows the platform's app SSO checklist; the
clinic-specific rules are marked below.

## Signing staff in

1. WorkflowHub hands the app the user's platform token. Its injected `/sso.js` publishes `window.__wfhSsoToken`, and
   the address carries `#sso_token=…`. The token is in the fragment, so it never reaches a server log.
2. `frontend/src/lib/platformSso.ts` takes the token and removes it from the address bar at once. It then posts the
   token to `POST /api/auth/platform-session`. `main.tsx` waits for this before the first render, so the app never
   flashes "signed out".
3. The backend (`backend/app/api/platform_sso.py`) asks the platform's auth service who the token belongs to:
   `GET {WFH_AUTH_SERVICE_URL}/tokens/details` with `X-Auth-Token`.
4. It then returns exactly what `/api/auth/login` returns: the same session token and the same response. Nothing else
   in the app can tell the two sign-ins apart, and the login form keeps working.

### Rules

- **Fail closed.** A forged or expired token, a refusal or an unreachable auth service all give 401. The user then
  sees the normal login form; an outage never creates a session.
- **Matching staff.** A platform account is matched to a clinic staff account by email.
- **Any platform user can run consultations.** A platform user with no clinic account (platform role `user`) gets
  an **operator** account on first sign-in: the clinic-device role that runs consultations. Doctors and admins are
  never created this way; they sign in with their clinic accounts (a doctor also needs a registration number).
  `WFH_DEFAULT_ROLE=` (empty) refuses unknown platform users instead (403: "Ask a clinic admin to add one").
- **Disabled accounts.** A disabled clinic account outranks a valid platform session (403).
- **Admins.** A platform admin is *not* a clinic admin.
- **Existing sessions.** An existing session in the browser wins over the platform token, since the person may have
  signed in as someone else on purpose.

### Behaviour in the browser

These follow the WorkflowHub "Building an Application" guide, using the gate's `window.__wfhSso.token()` helper:
- **First visit, reload, new tab, later visit:** signed in.
- **The platform user changes:** the app follows the new platform login.
- **Sign out in the app:** stays signed out for that platform login; a new platform login signs in again.
- **Expired session (401):** the app signs in again once with the current platform session and retries the call.

## The login form: platform email and password

Opened outside WorkflowHub, or after signing out, the form accepts the person's **WorkflowHub email and password**.
1. `POST /api/auth/login` first tries a clinic account's own password, so the demo and clinic accounts keep working.
2. Otherwise it signs in with the platform (`POST {WFH_AUTH_SERVICE_URL}/users/login`) and checks who the token
   belongs to (`/tokens/details`).
3. It then applies the same rules as automatic sign-in: matched by email to a clinic staff account, unknown platform
   users become operators, and disabled accounts are refused.

The platform session, with its refresh token, also becomes the model gateway's session (unless a service account is
set).

Answers:

| Case | Answer |
|---|---|
| Wrong email or password | 401 |
| No clinic staff account | 403, "Ask a clinic admin" |
| Auth service unreachable | 503, "Clinic accounts still work" |
| `WFH_AUTH_MODE=none` | the platform isn't asked at all |

Passwords and emails are never logged.

## Sharing the API with other apps and AI agents

`node.yaml` lists the shared endpoints under `api:` (base path `/api`):

| Key | Call | Who (usual role check) |
|---|---|---|
| `list_catalogue` | `GET /catalogue` | any clinic staff |
| `doctor_queue` | `GET /doctor/queue` | doctor, admin |
| `search_records` | `GET /doctor/records?q=&outcome=&limit=` | doctor, admin |
| `visit_summary` | `GET /visits/{visit_id}/summary` (approved plans only) | any clinic staff |
| `room_schedule` | `GET /ot/schedule?day=` | doctor, admin |
| `free_slots` | `GET /ot/availability?item_id=&day=&count=` | doctor, admin |
| `bookings_to_confirm` | `GET /ot/tentative` | doctor, admin |
| `get_booking` | `GET /ot/bookings/{booking_id}` | doctor, admin |
| `confirm_booking` | `POST /ot/bookings/{booking_id}/confirm` with `{"version"}` (texts the client) | doctor, admin |
| `room_report` | `GET /ot/reports?from=&to=` | doctor, admin |

How a shared call works:
- **As the person asking.** WorkflowHub calls the app as the person asking, never as the app's owner. It adds a
  signed `X-Wfh-User` header (HS256, issuer `workflowhub`, secret `WFH_APP_API_SECRET`, audience
  `WFH_APP_API_AUDIENCE`, all injected by WorkflowHub).
- **Checked in the backend.** `app/security.py` checks the signature, issuer, audience and expiry, then maps the
  person to their clinic staff account by email (the same rules as above). The endpoint's usual role check then
  applies, so an agent can do only what that person could do in the app.
- **Only listed endpoints.** The app also refuses `X-Wfh-User` on any endpoint that isn't listed, even if WorkflowHub
  forwarded it (`app/shared_api.py`; a test keeps it in step with `node.yaml`).
- **Off by default.** Without `WFH_APP_API_SECRET`, sharing is off (401 "App API is not enabled").
- **Audit.** Every shared call is logged as `[api] <key> called by <email> (<role>)`. Actions such as
  `confirm_booking` appear in the visit's audit trail under that person's name.

After uploading:
1. Open the app → **Sharing → Agent access (MCP) → Expose to agents**. While it is off, every call is refused.
2. Share the app with the people who should use it (Sharing → by email).
3. The **API** tab shows each endpoint's full URL, a ready `curl` command, and the `uses:` block another app pastes
   into its own `node.yaml`.

From a script, use the app's address plus the caller's platform token:

```bash
curl https://<app>.instaviv.in/api/ot/tentative -H "X-Auth-Token: <platform token>"
```

`GET /api/auth/platform-config` tells the web app whether SSO is on. `WFH_AUTH_MODE=none` turns it off; the
endpoint then answers 404.

## The model gateway token

The gateway (`ws://192.168.0.84:5007/ws/models/<project>/…?token=…&notify=1`) takes a platform access token. Those
tokens last 15 minutes, so `backend/app/integrations/platform_auth.py` keeps one renewed. It uses the first of these
sources that's available:

**The account matters, not just the token.** The gateway runs a model only for platform accounts allowed to use it.
On 26 October 2026, a regular account (`developer@dataviv.in`, platform role `user`) was refused for both models:
close code 4404, "no prediction model 187 you can use". So the app needs an account that can run models 187 and
167, or the models must be shared on WorkflowHub with the people who use the app. The doctor's review shows that
message when it happens.

1. **A service account** (`SKIN_MODEL_AUTH_EMAIL` and `SKIN_MODEL_AUTH_PASSWORD`): an account that can run models
   187 and 167. The app signs in with `POST /users/login` and renews the token with `POST /tokens/refresh`, signing
   in again if needed.
   - Every model call uses it, whoever is signed in. It works with nobody signed in and survives restarts, so it's
     **recommended for production**.
   - When it is set, staff sessions are never used for the models.
2. **The platform session of whoever signed in** (only useful if the models are shared with them):
   - **Platform email and password on the login form:** the platform returns a refresh token too, so the session
     renews itself.
   - **Automatic sign-in, platform admins:** the app calls `POST /tokens/handoff` (`{"client": "web"}`), which gives
     it its own access and refresh token for that admin.
   - **Automatic sign-in, everyone else:** the platform refuses the handoff ("Only admins can switch between the
     apps."). The app then uses the person's own token, which lasts 15 minutes. While the app is open in WorkflowHub
     it passes on the current token every 4 minutes (`POST /api/auth/platform-token`), so the models keep a valid one.
   - A session that can renew itself is never replaced by a browser token.
   - Sessions are kept in memory only, so after an API restart someone has to sign in once more.
   - `SKIN_MODEL_TOKEN_FROM_SSO=false` turns this off.
3. **A fixed token** (`SKIN_MODEL_TOKEN`), for quick checks only, because it expires.

**Renewal:**
- **Before expiry:** the token is renewed 60 s before it expires.
- **401/403 from the gateway:** the app renews the token and retries the connection once.
- **Renewal fails:** the doctor's review says why ("open the app from WorkflowHub, or set a service account"). A token
  problem never marks the model server as down.

**Only the gateway gets the token.** It goes only to addresses that ask for one (a `token=` parameter or a `{token}`
placeholder).

**The token is never shown.** Tokens never appear in logs, error messages or API responses. `GET /api/health` shows
only where the token comes from, how long it has left, and whether it can be renewed:

```json
"gateway_token": {"source": "platform session of Front Desk Kiosk", "expires_in_s": 842, "refreshable": true}
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `WFH_AUTH_SERVICE_URL` | `http://192.168.0.84:5001/api` | The platform's auth service (WorkflowHub injects it) |
| `WFH_AUTH_MODE` | `platform` | `none` turns SSO off |
| `WFH_DEFAULT_ROLE` | `operator` | Role for platform users with no clinic account. Empty: refuse them |
| `WFH_APP_API_SECRET`, `WFH_APP_API_AUDIENCE` | (injected) | Check `X-Wfh-User` on shared API calls; unset = sharing off |
| `SKIN_MODEL_AUTH_EMAIL`, `SKIN_MODEL_AUTH_PASSWORD` | (empty) | Service account for the model gateway (preferred) |
| `SKIN_MODEL_TOKEN_FROM_SSO` | `true` | Use the platform session from staff SSO sign-ins for the models |
| `SKIN_MODEL_TOKEN` | (empty) | Fixed gateway token (expires; checks only) |

## Deploying on WorkflowHub

1. Upload the zip, which excludes `node_modules`, `.git`, `__pycache__` and `dist`.
2. Ask the WorkflowHub operator to add the app to `SSO_EDGE_ENABLED_FEATURE_IDS` **as a slug glob**
   (`ai-skincare-assistant-*`), not a feature id: every re-upload gets a new id.
3. The operator restarts `celery_worker`, then `app`, then reloads nginx.

Check that a bad token is refused:

```bash
curl -sS -X POST http://<app>/api/auth/platform-session -H 'Content-Type: application/json' -d '{"token":"garbage-token"}'
# 401 {"detail":"Invalid or expired platform session"}
```

## Tests

`backend/tests/test_platform_sso.py` runs against a fake auth service that answers like the real one. It checks:
- sign-in for an existing staff member;
- 401 for a forged token, and for an auth service that's down (an outage gives no session);
- 409 for a platform account with no email;
- that unknown users are refused, created only as operators when allowed, and that a disabled account wins;
- that the model gateway gets a handed-over session that is renewed before expiry;
- a refused token renewed and retried once;
- that a service account is preferred over staff sessions;
- that health shows the token source and never the token.
