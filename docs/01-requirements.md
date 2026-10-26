# 01 · Software Requirements Specification

**Product:** AI Skincare Assistant (Skin Treatment, AI edition)
**Source:** *SKIN TREATMENT — Product Spec*, draft 12 Oct 2026 (based on Skin Treatment v26.0.2+1)
**Status:** MVP build covering all five delivery phases

## 1. Purpose

Today a doctor runs every step of a skin consultation by hand. This system lets an AI assistant run the consultation through voice or text, calling the same clinical and data services. A registered doctor still approves every prescription and takes over any case the AI escalates.

## 2. Users and roles

| Role | Who | What they do |
|---|---|---|
| **Client** | The person being consulted | Talks to the AI on a clinic device, where identity is checked by OTP on their mobile. Can also sign in on their own phone at `/my` with mobile + OTP to read their consultations and chat with the clinic (FR-21) |
| **Operator** | Clinic staff or kiosk device account | Starts AI sessions on a clinic device |
| **Doctor** | Registered medical practitioner (registration number stored) | Reviews drafts, approves, edits or rejects them, and takes over escalations |
| **Admin** | Clinic administrator | Manages users and the catalogue, and reads the audit log |

## 3. Functional requirements

IDs are traced to the spec section in brackets.

### Identity and visit
- **FR-01** [§02, §03.1] The AI collects the client's mobile number, sends an OTP and verifies it. Limits: 5 wrong attempts, 3 sends per 10 minutes, 5-minute expiry.
- **FR-02** [§02] After verification the AI finds the client in the local DB. If not found, it imports them from D365. If D365 doesn't have them either, it collects name, date of birth and gender and creates the client.
- **FR-03** [§02] The AI starts a visit. It picks the programme from the client's stated concern and their history, and sets the next session number in that programme.
- **FR-04** [§03.2] The AI records the concern in the client's own words and extracts structured concerns from it (acne, pigmentation, wrinkles and so on).

### Know your Skin
- **FR-05** [§02] The AI asks the 18-question *Know your Skin* questionnaire as a conversation. It skips questions that don't apply (for example, pregnancy for male clients) and reuses stable answers from an assessment in the last 180 days.
- **FR-06** [§02] The system works out skin type (dry, normal, combination or oily) and a sensitivity flag from the answers.

### Photos and analysis
- **FR-07** [§02] The AI guides capture of three poses (front, left, right). Each photo is checked for a face, framing, size, brightness and blur. The AI tells the user what to fix ("move closer", "find more light") and asks for a retake.
- **FR-08** [§02] Analysis (diagnosis and anti-ageing) runs automatically once all required photos pass. The AI then explains the results in plain words.
- **FR-09** [§02] For a returning client, the system compares the new results with the previous visit for the concerns the user raised and reports the change as a measured improvement.

### Prescription and approval
- **FR-10** [§02, §04] The system drafts a prescription (services, products, doctor services, session counts) **only from the clinic catalogue**, using the analysis, the concerns, skin type and the clinic rules.
- **FR-11** [§02] Safety checks run on every draft: contraindications against pregnancy, medication, sensitivity and allergies. Any blocked item is removed and the reason is recorded.
- **FR-12** [§05] Every prescription goes into a doctor review queue. The doctor can approve, edit and approve, or reject it. The client never sees a final prescription that a doctor hasn't approved.
- **FR-13** [§05] The AI hands the visit to a doctor when any of these is true:
  - the client is pregnant or breast-feeding;
  - the client is under 18;
  - the client takes medication that conflicts with treatment;
  - a photo shows something outside skincare;
  - analysis confidence is below the threshold;
  - the user asks for a person.

  These rules run **in code**, after every user message and every action.
- **FR-14** [§05] When a doctor takes over, they see the whole conversation, the photos, the results and the action log. They can message the client in the same session.

### Wrap-up
- **FR-15** [§02] The AI writes visit notes and tags from the conversation.
- **FR-16** [§02, §03.8] After approval the AI creates the visit summary, shares it (SMS or WhatsApp adapter, plus an expiring link) and sets the next session date.

### Platform
- **FR-19** *(added 17 Oct 2026)* After approval, each clinic treatment in the plan that needs a procedure room (OT) is booked before the visit closes. The AI offers the three earliest free slots in a room that suits the treatment; the client taps one, asks for later dates or names a day, or chooses to book later (the front desk then sees it on a to-book list). A slot is free when a suitable room is open for the whole treatment and no booking (plus turnover) or block overlaps it. Two bookings can never overlap in one room. Staff see a day calendar of all rooms, can book, cancel and block time, and admins manage rooms.
- **FR-20** *(added 17 Oct 2026)* Every consultation stays on record for doctors and admins after it leaves the review queue, whatever happened: approved, rejected, handed over, closed by a doctor, still in progress or stopped before a visit started. The record holds the full chat, the report (assessment, photos, findings), the plan and its versions, who decided what and when, hand-overs, treatment bookings, visit notes and the action log. Records can be searched by name or mobile and filtered by outcome. The clinic device can't open them.
- **FR-21** *(added 20 Oct 2026)* Clients can chat about any past consultation, and doctors and admins can chat with any client when needed. Each past visit has its own chat, plus one general chat per client. Clients use it on the clinic device (History tab, after OTP) or on their phone (`/my`, mobile + OTP). The AI answers only from that client's doctor-approved findings, plan and bookings; it never changes a plan or booking. A request for a person, a worrying symptom, a change request or a question the approved plan can't answer goes to the clinic team (the Review queue and the Messages tab; symptoms are listed with escalations). Once staff write, the AI stays quiet until they hand the chat back. Each staff reply texts the client once until they've read it. A client can never reach another client's chats, visits or summaries.
- **FR-22** *(added 21 Oct 2026)* Treatment booking checks people and machines, not just rooms, and tracks the treatment day:
  - **Doctors and machines.** A treatment can need a doctor (`needs_doctor`) and a machine type (`equipment_type`). A booking reserves a free doctor who is on shift (the approving doctor first) and a free, in-service machine of that type, plus the machine's cool-down or sterilisation time. A doctor can't be in two treatments at once, and a machine can't serve two rooms at once. Doctors have weekly working hours; leave and machine servicing are blocks.
  - **Holidays and overrides.** No times are offered on a clinic holiday, outside room hours or when no doctor is on shift. Doctors and admins can book those anyway with a written reason, which is recorded. Overlaps, blocks, leave, a missing machine or an unsuitable room can never be overridden.
  - **Tentative bookings.** A time the client picks in the chat is held as tentative: the client is told it is provisional, and staff confirm it from the room calendar, which texts the client. Staff bookings are confirmed straight away.
  - **Readiness checklist.** Each booking has a pre-treatment checklist: consent signed, a patch test for lasers and peels, before photos (ticked automatically from the consultation photos), health re-check today, skin prep, and aftercare (optional). An item can be marked not needed or blocked only with a reason. Staff check the client in, start the treatment (only when the booking is confirmed and every required item is done or not needed), finish it, or mark a no-show. Times can be back-dated up to 12 hours.
  - **Safe edits.** Booking and moving accept an `Idempotency-Key`, and a retry with a different body is refused. Every change carries the booking's version, so a change made from an out-of-date screen is refused, and a failed move leaves the original booking untouched.
  - **Reports.** Booked and actual utilisation per room, turnover (finish to next start), on-time starts, overruns, no-shows, cancellations with reasons, overrides, and breakdowns by treatment, machine and doctor, for any date range, with a CSV export.
- **FR-17** [§06] Every AI action from the spec's action table is a typed tool with a matching REST endpoint.
- **FR-18** [§07] There are four screens: AI session, Results, Summary and Doctor review. A login screen is added for staff.

## 4. Non-functional requirements

These are the spec's §08 must-haves plus engineering defaults.

| ID | Requirement | How it's met |
|---|---|---|
| **NFR-01** | Consent to the AI and to recording is captured before the session starts | `POST /conversations` rejects requests without both consents and stores `consent_at` |
| **NFR-02** | Every AI action is logged with its inputs, result and reason | The single `ActionLayer` writes an `ai_actions` row for every call, including blocked and failed ones. Every tool requires a `reason` argument |
| **NFR-03** | No prescription reaches the user without doctor approval | The prescription state machine and the summary/share endpoints refuse anything that isn't `approved` |
| **NFR-04** | Escalation is enforced in code | `services/escalation.py` is deterministic and called by the orchestrator, not by the LLM. Once escalated, the AI can't act |
| **NFR-05** | HTTPS, auth on every endpoint, no secrets in the repo | JWT on every route except health and the token-gated public summary. Secrets come from env. TLS ends at the reverse proxy |
| **NFR-06** | No health data on the device, in logs or in analytics | The frontend keeps state in memory only (the auth token is in sessionStorage). API responses send `Cache-Control: no-store`. A log filter redacts phone numbers and emails, and no request bodies are logged. There's no analytics SDK |
| **NFR-07** | Exactly-once submission; limited retries | `Idempotency-Key` on mutating submissions, unique `client_message_id` per conversation message, and state-machine guards. Integration retries are capped at 3, and LLM tool loops at 6 steps |
| **NFR-08** | Pluggable AI | LLM provider set by env (Groq by default; any OpenAI-compatible API including Ollama; offline `scripted` fallback). Analysis, D365, OTP and SMS sit behind adapters |
| **NFR-09** | Performance | Non-LLM API p95 under 300 ms. The AI turn is bounded by the LLM provider (Groq is typically under 1.5 s) |

## 5. Out of scope for the MVP

- Real skin-analysis model integration. The adapter and a mock engine are provided.
- Real D365, SMS and WhatsApp providers. Adapters and mocks are provided.
- Integrating the real-time talking-avatar stream. The UI has a slot for it, and browser speech recognition and synthesis are used meanwhile.
- Billing and multi-clinic tenancy. General appointment booking (consultations, follow-ups) stays out; only treatment-room booking for an approved plan is in (FR-19). Later sessions of a course are booked at each visit, and only doctors' calendars are checked (therapists without a login aren't modelled).

## 6. Open issues for the product owner

1. **Telemedicine Practice Guidelines 2020.** AI may not counsel or prescribe. Two things follow:
   - Explaining the *draft* plan before a doctor approves it may count as counselling. This is controlled by `CLIENT_SEES_DRAFT_PLAN`.
   - Legal must confirm the default.
2. **Escalation thresholds.** The analysis-confidence threshold (`ESCALATION_MIN_CONFIDENCE`, default 0.6) and the medication-conflict list need clinical sign-off.
3. **Retention under the DPDP Act 2023.** Retention periods for transcripts and photos aren't defined yet.
4. **Questionnaire.** The real 18-question *Know your Skin* content needs to replace the placeholder in `app/knowledge/questionnaire.json`.
