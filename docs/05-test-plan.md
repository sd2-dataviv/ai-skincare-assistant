# 05 · Test Plan and Results

## Strategy

| Level | Tool | What it covers |
|---|---|---|
| Unit | pytest | Mobile parsing, answer parsing, skin-type scoring, concern detection, photo quality gates, improvement comparison, log redaction |
| API and integration | pytest + httpx (ASGI) on **real PostgreSQL** | Auth and role guards, no-store headers, every REST action endpoint, idempotency |
| Flow | pytest, driving the API like the app does | Full consultation (spec §03 steps 1–8), new-client profile, wrong OTP, returning client (reused answers, session 2, comparison) |
| Safety | pytest | Pregnancy, under-18, user asks for a person, medication conflict, low confidence and non-skincare finding all hand over. Doctor reject, then hand back, then redraft. Catalogue-only check. AI can't skip stages |
| LLM agent | pytest with a fake OpenAI-compatible provider | Tool calls go through the action layer with reasons. Out-of-stage tools are blocked. Provider failure falls back to the scripted agent |
| End-to-end | `e2e/run.mjs` (playwright-core + system Chrome, fake camera) | Consent, identification, questionnaire, camera, analysis and draft, results tab, doctor approval, client notified, summary, hand-over and doctor chat, phone layout |

The tests run with `LLM_PROVIDER=scripted` (deterministic), the mock adapters, and `PHOTO_FACE_CHECK=false`, because the test images have no real face. Face detection is exercised manually with a real camera.

## Traceability

| Requirement | Tests |
|---|---|
| FR-01 OTP | `test_full_consultation_happy_path`, `test_wrong_otp_then_right`, `test_message_exactly_once` |
| FR-02 find, import or create | happy path (D365 import), `test_new_client_profile_capture`, `test_rest_actions_mirror_ai_actions` |
| FR-03 / FR-04 visit, service, concerns | happy path (Acne Scription), returning client (session 2), `test_concern_detection_and_programme`, `test_services.py` (each service picked from the client's words, follow-up names, old visits still counted, Anti Aging adds the doctor's consultation) |
| FR-05 / FR-06 Know your Skin | `answer_all` in the flow tests, `test_parse_answer_variants`, `test_skin_type_derivation`, reuse in the returning-client test |
| FR-07 guided capture | `test_quality_gates`, retake after a dark photo (happy path), e2e camera |
| FR-08 / FR-09 analysis, improvement | happy path, `test_returning_client_compares_and_reuses_answers`, `test_compare_direction` |
| FR-10 / FR-11 catalogue-only, safety | `test_catalogue_only_and_role_guards`, `test_medication_conflict_escalates_and_excludes_items` |
| FR-12 doctor approval | happy path, `test_catalogue_only_and_role_guards` (edit, exactly once, 409) |
| FR-13 escalation rules | 6 tests in `test_escalation_and_safety.py` |
| FR-14 doctor take-over | `test_pregnancy_hands_over_and_blocks_ai`, e2e steps 15–16 |
| FR-15 / FR-16 notes, summary, next session | happy path (notes tags, SMS link, public summary, next date, completed) |
| FR-19 treatment-room booking | `test_ot.py`: suitable rooms only, opening hours, lead time and spread, turnover and blocks, no double booking (in code and by the DB constraint), date phrases, later dates / named day / pick by position, two treatments in a row, slot taken between offer and tap, book later → front desk list, staff calendar, booking, cancel, blocks and roles, staff booking moves the chat on, product-only plans skip booking, LLM free text in booking; happy path books and checks the SMS and summary |
| FR-21 follow-up chats | `test_threads.py`: phone sign-in (unknown numbers get the same answer and no text), client and staff tokens can't cross; the AI answers plan and appointment questions from approved data; unapproved visits stay hidden; change requests and symptoms go to the clinic team (symptoms never reach the LLM); the AI stays quiet once staff write; one SMS per unread burst; hand back; an admin starts a chat with any client, seen on the clinic device with an unread badge; clients never reach each other's chats; retried sends stored once; LLM replies claiming changes are cut. Browser run `e2e/chats.mjs` (8 steps) |
| FR-22 doctors, machines, confirmation, readiness, reports | `test_ot_resources.py`: one machine serves two rooms but one treatment at a time, out-of-service machines, machine servicing, a doctor can't be in two rooms (in code and by the DB constraint), off-shift and holidays need a written override but leave never does, chat bookings are tentative until confirmed (SMS, stale version refused), moves are checked like new bookings and a failed move keeps the original, a retried booking books once, the checklist gates the start and the report uses recorded times (utilisation, turnover, on-time, "—" for nothing to measure, CSV), setup is admin-only. Browser run `e2e/ot.mjs` (9 steps) |
| FR-20 records | `test_records.py`: a completed visit leaves the queue but stays on record with transcript, report, decisions and notes (doctor and admin); rejected-then-prescribed shows both decisions; a pre-visit under-18 hand-over is on record without leaking into another client's record; search by name and mobile; the kiosk gets 403. e2e step opens a finished record |
| FR-17 actions = endpoints | `test_rest_actions_mirror_ai_actions`, `test_llm_agent_calls_tools_through_action_layer` |
| NFR-01 … NFR-07 | See the table in `06-deployment-security.md` |

## Results (17 Oct 2026, aman-desktop, Python 3.12, PostgreSQL 16.x, Chrome stable)

| Suite | Result |
|---|---|
| Backend pytest | **116 passed** in about 57 s (21 Oct 2026) |
| Frontend `tsc` + `vite build` | Passed, with no type errors |
| Browser e2e, rooms (`node e2e/ot.mjs`) | **PASS**, 9 steps on 21 Oct 2026: a chat booking shows as tentative, is confirmed (client texted), checked in, start stays locked until the checklist is done, started and finished; setup and reports; phone width |
| Browser e2e, chats (`node e2e/chats.mjs`) | **PASS**, 8 steps on 20 Oct 2026 against the real Groq model: kiosk visit chat, doctor's New message, unread badge on the clinic device, phone sign-in and reply at 390 px, doctor sees the reply |
| Browser e2e (`make e2e`) | **PASS**, 19 steps incl. booking treatment rooms in the chat, the summary's appointments, the room calendar and a phone-width check; screenshots in `e2e/screenshots/` |

## Independent review

A separate reviewer read the code against these requirements and reported 12 issues. Each was fixed and now has a regression test:

| Finding | Fix | Test |
|---|---|---|
| "Close without AI" left the visit stuck; wrap-up was blocked | An approved plan always moves the stage to wrap-up. Approval resets the mode to `ai` and resolves all open escalations. Visits held by a doctor stay in the queue (`with_doctor`) | `test_close_without_ai_then_prescribe_still_wraps_up`, `test_pregnancy_hands_over_and_blocks_ai` |
| Hand-back matched other clients' pre-visit escalations (`IS NULL`) | The resolve query only uses ids that exist | `test_hand_back_only_affects_its_own_conversation` |
| Staff REST escalations didn't stop the AI | An escalated visit means the stage is `handed_over`. REST checks find the live conversation. Drafting and approval requests refuse escalated visits unless the doctor is acting | `test_staff_rest_escalation_stops_the_ai` |
| Drafts were readable by the kiosk account | `GET /visits/{id}` and `/recommendations` are doctor/admin only | `test_rest_actions_mirror_ai_actions` |
| Reject, then hand back, resubmitted the same plan | Hand-back after a rejection is refused (409); the doctor uses Prescribe | `test_doctor_reject_then_prescribe` |
| Unexpected errors weren't audited and lost the turn | `ActionLayer` logs every exception as an `error` row. The orchestrator always answers. LLM calls can't use non-LLM tools | `test_bad_llm_arguments_are_audited_not_crashing` |
| The scripted fallback could save a wrong answer after a partial LLM turn | The fallback runs only if no action succeeded in that turn | Covered in the LLM agent tests |
| Concurrent double actions | Row locks in `decide()`. The idempotency key is claimed before the work runs. A per-conversation turn lock is held for the whole turn | `test_catalogue_only_and_role_guards` (exactly once + 409) |
| The under-18 rule passed when the date of birth was unknown | Date of birth is collected at identification when missing | `test_unknown_date_of_birth_is_collected` |
| OTP and mobile number in the audit log | Inputs are redacted before logging | Assertion in the happy-path test |
| Tracebacks could leak data into logs | `exc_text` is redacted, and the engine uses `hide_parameters=True` | `test_log_redaction` |
| `take_over` without a conversation wasn't committed; operator escalations were mislabelled | Commit added; staff escalations are now `staff_requested` | Covered in the escalation tests |

The per-conversation turn lock is in-process. With more than one API worker, switch it to a Postgres advisory lock or Redis.

## Not covered yet

- Real Groq calls are checked with `python -m scripts.llm_smoke` (backend/). It runs a full new-client visit against the test database: free-text answers, a side question, "done" with no photo, 3 uploads and doctor approval. The last run on 16 Oct had 33 actions, 0 failed, and no garbled replies.
- Speech recognition and synthesis. These are browser features; check them on the actual kiosk device and its browser.
- Load testing. Turn latency is dominated by the LLM. Measure it against Groq at the expected number of concurrent sessions.
