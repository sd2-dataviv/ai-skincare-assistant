# 09 · The talking avatar

On the clinic device, the clinic's AI Avatar service (`wss://avatar.instaviv.in`) gives the consultation a face and a
voice. The app's own assistant still decides every word. The avatar runs in the service's **Direct** mode, where it
says the text it is sent, word for word. The service's own AI (its LLM, MCP and course modes) is never used.

## How it fits together

```
browser (consultation screen)                 API (FastAPI)                         AI Avatar service
  canvas + audio  <── frames ────────  /api/avatar/ws  relay  <── frames ──────  /{user}/ws?token=…&avatar_id=14
  {"type":"speak","text":…}  ───────>   checks, cleans  ───── plain text ──────>  Direct mode: says it
```

1. **Ticket.** The consultation screen asks `POST /api/avatar/session` (operator or admin) for a ticket. A ticket
   lasts 60 s and opens one socket.
2. **Relay socket.** The browser opens `WS /api/avatar/ws?ticket=…`.
3. **Opening the avatar session.** The API signs in to the avatar service with the platform service account, the
   same one the skin models use (`SKIN_MODEL_AUTH_EMAIL`, `SKIN_MODEL_AUTH_PASSWORD`). It then sends `language`,
   `{"type":"mode","value":"direct"}` and `stop`. The stop silences the greeting the service otherwise plays on
   connect.
4. **Saying replies.** Each assistant or doctor message is sent as `{"type":"speak","text":…}`, one at a time. A new
   line waits for `avatar-speak-completed`, because text sent mid-sentence would cut the avatar off.
5. **Drawing.** Frames come back on the same socket:
   - SyncTalk engine frames, prefixed `0x20`, are drawn by the ported SyncTalk player (`frontend/src/avatar/synctalk`).
     The idle loop comes from the atlases in `frontend/public/avatar/avatars`.
   - When the engine is busy, the service's own JPEG and PCM frames are drawn by `MuseTalkPlayer.ts`.

## What the relay allows

| Direction | Allowed | Dropped |
|---|---|---|
| Browser to avatar | `speak` (markdown, bullets and links removed; capped at 1 200 characters at a sentence end), `stop`, `language` (the service's languages) | Mode switches, raw queries, audio, anything else |
| Avatar to browser | Video and audio frames; `session_state`, `synctalk`, `avatar_reply`, `avatar-speak-completed`, `avatar_error`, `status`, `audio_control`, `emotion` | The greeting, course events, MCP routing frames |

So the kiosk can't turn the avatar into a general chatbot, and the platform token never reaches the browser, a log
or an error message.

## When the avatar can't be used: voice-only mode

A Siri-style voice orb takes the avatar's place, with a small **Voice only** label, and the replies are still spoken:
1. **The avatar's own voice.** `POST /api/avatar/speech` (operator or admin) asks the same service's speech engine
   (`/tts/preview`, voice `AVATAR_VOICE`, default F1 = Meera) for the reply as a WAV. It needs no platform sign-in, so
   it keeps working when the account is at its device limit. Measured on 29 Oct 2026: 21 s of speech in 0.4 s.
2. **The browser's own voice**, when that engine can't be used (404 not set up, 503 or no answer). The engine is
   tried again after a minute.

The orb follows the voice: it swells with the real audio level of the avatar's voice (Web Audio analyser), with a
lively stand-in for the browser's voice, pulses a ring while listening and turns slowly while the assistant thinks.
`node e2e/voice-only.mjs` checks both layers in a browser (screenshots `voice-1-speaking.png`, `voice-2-idle.png`).

Voice-only mode is used in all of these cases:

| Situation | What happens |
|---|---|
| `AVATAR_WS_URL` empty | `/api/avatar/session` answers 404. The orb with the browser's voice. |
| No platform session (e.g. the account at its device limit) | Relay closes 4503 with the reason. Voice-only mode; **Retry avatar** button. |
| Account refused (401/403, or close 4401/4403) | One retry with a renewed token, then 4502. Voice-only mode; **Retry avatar**. |
| Avatar missing or inactive (4404/4503 from the service) | 4504. Voice-only mode; **Retry avatar**. |
| Service dropped | Three quick reconnects (1, 2, 4 s), then voice-only mode. |
| Nothing said for 10 minutes | 4408: the session is freed on the GPU. It reconnects for the next reply. |

Lines queued when the avatar gives up are spoken in voice-only mode, so nothing is lost.

**Behaviour on the clinic device:**
- Pressing the microphone, or **Voice off**, stops the avatar mid-sentence.
- The canvas stays mounted while the camera is on for photos, so the session isn't dropped between questions.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `AVATAR_WS_URL` | (empty: off) | The avatar service, e.g. `wss://avatar.instaviv.in` |
| `AVATAR_ID` | `14` | The service's catalogue: 14 Meera, 10 Raghav, 18 Vedant Sir |
| `AVATAR_LANGUAGE` | `en` | Voice language (the consultation runs in English) |
| `AVATAR_MAX_SPEECH_CHARS` | `1200` | Longer replies are cut at a sentence end |
| `AVATAR_CONNECT_TIMEOUT_S` | `10` | Time allowed to open the avatar session |
| `AVATAR_IDLE_CLOSE_S` | `600` | Free the avatar after this long with nothing said |
| `AVATAR_VOICE` | `F1` | Voice for voice-only mode (the service's Supertonic voices F1–F5, M1–M5); empty = browser voice |
| `AVATAR_TTS_TIMEOUT_S` | `20` | Time allowed for the speech engine |

`GET /api/health` shows `"avatar": {"service", "avatar_id", "language", "mode"}`, never the token.

**Deploying:**
- **Docker:** the web container's nginx forwards the WebSocket upgrade for `/api/avatar/ws` and caches
  `/avatar/` files.
- **WorkflowHub:** its edge proxy must pass WebSocket upgrades to the app. Check this after the first upload: the
  consultation shows the avatar, and `[avatar] session opened` appears in the API log.

## Checked on 28 October 2026

Live service, avatar 14 (Meera), the SyncTalk renderer:
- **Probe (`var/avatar_probe.py`):**
  - Without a mode flag, the session starts in "teacher" mode and greets ("I'm Ai-Trainer…").
  - Direct mode plus `stop` silences the greeting.
  - Each text is said exactly (`avatar_reply` with `mode: direct`), with the first frames about 1 s after sending.
  - `is_mcp=true` sessions ignore Direct mode and send text to Workflow Hub, so the app doesn't use them.
- **Relay (`var/avatar_relay_check.py`):**
  - Two lines were said in order through the Vite proxy.
  - A `mode` switch from the browser was dropped.
- **Browser (`node e2e/avatar.mjs`, headless Chrome):**
  - The avatar was on screen 1.4 s after the consultation opened, and the greeting was said by the avatar.
  - The reply to the mobile number started 1.3 s after sending.
  - The browser's own voice was never used.
  - Screenshots: `e2e/screenshots/avatar-1-greeting.png` and `avatar-2-reply.png`.

## Tests

`backend/tests/test_avatar.py` runs against a fake avatar service. It checks:
- Direct mode, the stopped greeting, and line cleaning;
- dropped mode switches and queries;
- filtered frames, and that the token never reaches the browser;
- a refused token renewed once;
- the refusal close codes and the idle close;
- the ticket: 60 s, one use, operator or admin only.
