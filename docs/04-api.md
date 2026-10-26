# 04 · API Reference

The interactive OpenAPI docs are at **`/api/docs`** while the API is running. Every route needs `Authorization: Bearer <JWT>` except `/api/health`, `/api/auth/login` and the token-gated `/api/public/summaries/{token}`.

## AI actions (spec §06)

Every AI action is a typed tool in `app/ai/tools.py`, and each one has a matching REST endpoint for staff and integrations.

| Action | Replaces | REST endpoint | Stage |
|---|---|---|---|
| `send_otp` / `verify_otp` *(added)* | Part of the D365 lookup | Inside the conversation | identify |
| `find_or_import_client` | Search and D365 import | `GET /api/clients?mobile=`, `POST /api/clients/import`, `POST /api/clients` | identify |
| `start_visit` | Select Service + Next | `POST /api/clients/{id}/visits` | concern |
| `save_assessment` | Know your Skin forms | `PUT /api/visits/{id}/assessment` | assess |
| `upload_photo` | Camera ✓ | `POST /api/visits/{id}/photos`, `POST /api/conversations/{id}/photos` | capture |
| `run_analysis` | Process image / Compare | `POST /api/photos/{id}/analyses` | analyse |
| `get_recommendations` | Expert Services list | `GET /api/visits/{id}/recommendations` | recommend |
| `draft_prescription` | Selected Data + Submit | `PUT /api/visits/{id}/prescription` | recommend |
| `request_approval` *(new)* | | `POST /api/visits/{id}/approval` | recommend |
| `escalate_to_doctor` *(new)* | | `POST /api/visits/{id}/escalate` | any active stage |
| `add_note` | Client Notes + | `POST /api/clients/{id}/notes` | after identification |
| `share_summary` *(new)* | | `POST /api/visits/{id}/summary/share` | wrap_up, done |
| `set_next_session` *(added)* | | Runs automatically at wrap-up (first booked treatment, else plan interval) | wrap_up |
| `offer_ot_slots` *(new)* | | `GET /api/ot/availability?item_id=&day=&part=&count=` | booking |
| `book_ot_slot` *(new)* | | `POST /api/ot/bookings` | booking |
| `defer_ot_booking` *(new)* | | Front desk books later from `GET /api/ot/schedule` → `to_schedule` | booking |

The LLM sees each tool with a required `reason` argument, which goes into `ai_actions.reason`.

## Conversation (clinic device)

| Method and path | Purpose |
|---|---|
| `POST /api/conversations` | Body `{consent_ai, consent_recording, language}`. Returns 400 unless both consents are true. Returns the greeting and UI hints |
| `GET /api/conversations/{id}?after_seq=N` | New messages and the current UI hints. Used for polling during review or hand-over |
| `POST /api/conversations/{id}/messages` | `{text, client_message_id}`. Exactly once per `client_message_id`; a duplicate replays the first replies |
| `POST /api/conversations/{id}/photos` | Multipart `pose` + `file`. Returns quality feedback. The last good photo triggers analysis, drafting and the approval request |
| `GET /api/conversations/{id}/results` | Photos, findings, skin age, comparison, and the draft plan (only if `CLIENT_SEES_DRAFT_PLAN`) |

The **UI hints** returned on every turn are:
- `stage`, `mode`, `step`
- `input`: `tel`, `otp`, `date` or `text`
- `quick_replies`
- `question`: the next *Know your Skin* question, with its index and total
- `camera`: the next pose, a prompt, and the poses done and still missing
- `results_available`, `approval_status`, `summary_available`
- `booking` (booking stage only): the treatment being booked, its duration, sessions, how many are left to book, and the offered `slots`. The slot labels are also the `quick_replies`, followed by "Later dates" and "Book later"

## Doctor

| Method and path | Purpose |
|---|---|
| `GET /api/doctor/queue` | Pending approvals and open escalations. Escalations come first, then oldest first |
| `GET /api/visits/{id}` | Full visit: client, assessment, photos, analyses, plan, approvals, escalations, treatment bookings, visit notes, the names of the doctors involved (`people`) and the action log |
| `POST /api/visits/{id}/approval/decision` | `{decision: approve, edit or reject, items?, notes?}`. Supports `Idempotency-Key`. Approving triggers the AI wrap-up |
| `POST /api/doctor/visits/{id}/prescribe` | The doctor writes the plan and approves it in one step (used after a take-over) |
| `POST /api/doctor/escalations/{id}/take-over` | Assigns the case and posts "Dr X has joined" |
| `POST /api/doctor/escalations/{id}/resolve` | `{hand_back_to_ai}`. Handing back resumes the AI where it stopped |
| `GET /api/doctor/conversations/{id}`, `POST …/messages` | Read the transcript (with the client and the hand-over history) and message the client |
| `GET /api/doctor/records?q=&outcome=&limit=&offset=` | Every consultation, newest first, whatever its outcome: completed, approved, awaiting approval, handed to a doctor, plan rejected, in progress, or stopped before a visit started. `q` matches the client's name or part of the mobile. Each row has the outcome, the doctors' decisions, hand-overs, who handled it and treatments booked. Doctors and admins only |

## Follow-up chats (FR-21)

| Method and path | Who | Purpose |
|---|---|---|
| `POST /api/client-auth/start` | anyone | `{mobile}`. Texts a code if the number is a client's; the answer is the same either way |
| `POST /api/client-auth/verify` | anyone | `{mobile, code}`. Returns a client token (`role=client`, 7 days) |
| `GET /api/me`, `GET /api/me/history`, `GET /api/me/visits/{id}/summary` | client | Profile with unread count; own consultations (approved details only) and upcoming treatments; an approved summary |
| `GET/POST /api/me/threads`, `GET /api/me/threads/{id}?after_seq=N`, `POST …/messages` | client | List chats; open the chat for `{visit_id}` (or `null` = general); read (marks read); send `{text, client_message_id}`, exactly once |
| `GET/POST /api/conversations/{id}/threads…` | clinic device | The same for the client verified in that conversation (403 before OTP) |
| `GET /api/messages/threads?waiting=&client_id=&q=` | doctor, admin | Every chat, waiting ones first, with unread counts |
| `POST /api/messages/threads` | doctor, admin | `{client_id, visit_id?}`. Open (or find) a chat with any client |
| `GET /api/messages/threads/{id}?after_seq=N`, `POST …/messages`, `POST …/hand-back` | doctor, admin | Read; reply (the AI stops answering; the client is texted once until they read); hand back to the AI |

Another client's thread, visit or summary answers 404. Chats waiting for staff appear in `GET /api/doctor/queue` as `type: "message"` (`urgent` for symptoms, listed with escalations). UI hints include `unread_messages`.

## Treatment rooms (OT)

| Method and path | Who | Purpose |
|---|---|---|
| `GET /api/ot/rooms` | staff | Rooms with capabilities, opening hours, open days and turnover buffer |
| `POST /api/ot/rooms`, `PATCH /api/ot/rooms/{id}` | admin | Add or change a room (deactivate with `active: false`) |
| `GET /api/ot/schedule?day=YYYY-MM-DD` | doctor, admin | One day of the calendar in clinic time: each room's bookings (with client name, status, stage, doctor and readiness) and blocks, doctors' leave and machine servicing that day, the holiday name if any, `to_schedule` (approved treatments with no room booked) and `to_confirm` (tentative chat bookings) |
| `GET /api/ot/availability?item_id=&day=&part=&count=` | doctor, admin | Earliest free slots for a treatment in suitable rooms |
| `POST /api/ot/check` | doctor, admin | Dry run `{item_id, room_id, starts_at, doctor_id?, equipment_id?, booking_id?}`: every conflict, whether it can be overridden, and the doctor and machine that would be used |
| `POST /api/ot/bookings` | doctor, admin | `{visit_id, item_id, room_id, starts_at, doctor_id?, equipment_id?, override_reason?}`. Confirmed straight away. `Idempotency-Key` (a different body with the same key is 422). A conflict returns 409/422 with `conflicts[]` and `needs_override`; a waiting chat moves on by itself |
| `GET /api/ot/bookings/{id}` | doctor, admin | A booking with its doctor, machine, checklist, readiness and `version` |
| `PATCH /api/ot/bookings/{id}` | doctor, admin | Move: `{version, starts_at, room_id?, doctor_id?, equipment_id?, override_reason?}`. 409 `stale_version` if someone changed it first; a failed move keeps the original |
| `POST /api/ot/bookings/{id}/confirm` | doctor, admin | `{version}`. Confirms a tentative (chat) booking and texts the client |
| `POST /api/ot/bookings/{id}/stage` | doctor, admin | `{stage: checked_in, in_progress, completed or no_show, version, at?}`. Starting needs a confirmed booking and a complete checklist (409 `not_ready`) |
| `PATCH /api/ot/bookings/{id}/readiness/{key}` | doctor, admin | `{status: pending, done, not_applicable or blocked, note?}`; a note is required for not applicable and blocked |
| `POST /api/ot/bookings/{id}/cancel` | doctor, admin | `{reason?, version?}`. The treatment goes back on `to_schedule` |
| `GET /api/ot/tentative` | doctor, admin | Upcoming chat bookings waiting for confirmation (also `to_confirm` in the schedule) |
| `GET/POST /api/ot/blocks`, `DELETE /api/ot/blocks/{id}` | doctor, admin | Block a room, a doctor (leave) or a machine (servicing): `{room_id or doctor_id or equipment_id, starts_at, ends_at, reason}`. 409 `conflicts_with_bookings` if bookings are in the way |
| `GET /api/ot/equipment`; `POST`, `PATCH /api/ot/equipment/{id}` | staff; admin | Machines: list, add, change buffer, take out of service |
| `GET /api/ot/doctors`; `PUT /api/ot/doctors/{id}/hours` | doctor, admin; admin | Doctors with weekly hours; replace a doctor's hours `{hours: [{weekday, start_time, end_time}]}` |
| `GET /api/ot/holidays`; `POST`, `DELETE /api/ot/holidays/{day}` | staff; admin | Clinic holidays; adding one lists the bookings already on that day |
| `GET /api/ot/reports?from=&to=&room_id=`, `GET /api/ot/reports.csv` | doctor, admin | Utilisation, turnover, punctuality and outcomes from recorded data (default: last 30 days); CSV has no client data |
| `GET /api/ot/visits/{id}/bookings` | doctor, admin | A visit's bookings and deferrals |

The clinic device (operator account) faces the client, so it can't open the calendar, which shows other clients' names. It books a client's own treatments only through the chat. Times are ISO 8601 with an offset; the calendar and slot labels use `CLINIC_TIMEZONE` (default `Asia/Kolkata`).

## Errors

Domain errors return `{"detail": "<plain sentence>", "code": "<machine_code>"}`. The status codes are:

| Status | Meaning |
|---|---|
| 409 | State conflict, for example the plan is already approved |
| 422 | Validation, for example `not_in_catalogue` |
| 403 | Role, or a summary requested before approval |
| 429 | OTP rate limit |
