/**
 * The talking avatar on the consultation screen. The assistant's replies are sent to the clinic's AI Avatar service
 * (through the API's relay, /api/avatar/ws), which says them word for word in its Direct mode; the face and voice
 * come back on the same socket.
 *
 * Two ways the service draws the face, announced in `session_state.renderer`:
 *   synctalk  the SyncTalk engine's frames, relayed with a 0x20 prefix, plus its control messages
 *             (`{"type":"synctalk","msg":{...}}`): drawn by the ported SyncTalkPlayer;
 *   musetalk  the service's own JPEG + PCM frames: drawn by MuseTalkPlayer.
 *
 * Replies are spoken one at a time: text sent while the avatar is talking cuts the current sentence off on the
 * service, so the next one waits for `avatar-speak-completed`.
 */
import { api, ApiError } from "../lib/api";
import { MuseTalkPlayer } from "./MuseTalkPlayer";
import { SYNCTALK_RELAY_PREFIX, syncTalkAvatarFor } from "./synctalk/avatars";
import { SyncTalkPlayer } from "./synctalk/SyncTalkPlayer";

export type AvatarState =
  /** No avatar set up on this installation: the browser's voice speaks. */
  | "off"
  | "connecting"
  | "ready"
  /** Closed after a quiet spell; it reconnects for the next reply. */
  | "resting"
  /** Not available now (refused, unreachable, gave up): the browser's voice speaks. */
  | "unavailable";

export interface AvatarEvents {
  onState: (state: AvatarState, detail?: string) => void;
  onSpeaking: (speaking: boolean) => void;
}

type Ticket = { ticket: string; avatar_id: number; language: string; path: string };

const MAX_RETRIES = 3;
/** A reply the service never finished (no `avatar-speak-completed`) stops blocking the queue after this. */
const TURN_TIMEOUT_MS = 90000;
const MAX_QUEUE = 6;

// Close codes from the API relay (backend/app/integrations/avatar.py).
const CLOSE_BAD_TICKET = 4401;
const CLOSE_IDLE = 4408;
const CLOSE_REFUSED = 4502;
const CLOSE_NO_SESSION = 4503;
const CLOSE_UNAVAILABLE = 4504;

export class AvatarSession {
  private readonly canvas: HTMLCanvasElement;
  private readonly events: AvatarEvents;
  private ws: WebSocket | null = null;
  private state: AvatarState = "connecting";
  private avatarId = 14;
  private renderer: "synctalk" | "musetalk" | null = null;
  private sync: SyncTalkPlayer | null = null;
  private muse: MuseTalkPlayer | null = null;
  private queue: string[] = [];
  private inFlight = false;
  private turnTimer: ReturnType<typeof setTimeout> | null = null;
  /** When the current reply was sent: a "completed" this soon after is the stopped reply's, not this one's. */
  private sentAt = 0;
  private retries = 0;
  private ticketRetried = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  constructor(canvas: HTMLCanvasElement, events: AvatarEvents) {
    this.canvas = canvas;
    this.events = events;
  }

  get current(): AvatarState {
    return this.state;
  }

  /** True when replies should go to the avatar rather than the browser's voice. */
  get usable(): boolean {
    return this.state === "connecting" || this.state === "ready" || this.state === "resting";
  }

  start(): void {
    void this.connect();
  }

  /** Say these lines, after anything already queued. */
  speak(text: string): void {
    const t = text.trim();
    if (!t || this.disposed) return;
    this.queue.push(t);
    if (this.queue.length > MAX_QUEUE) this.queue.shift();
    if (this.state === "resting") void this.connect();
    else this.pump();
  }

  /** Stop talking now and forget what is queued (the client started speaking, or voice was turned off). */
  stop(): void {
    this.queue = [];
    this.endTurn();
    this.sync?.stop();
    this.muse?.stop();
    this.send({ type: "stop" });
    this.events.onSpeaking(false);
  }

  /** Lines not yet spoken (handed to the browser's voice if the avatar gives up). */
  takeQueue(): string[] {
    const q = this.queue;
    this.queue = [];
    return q;
  }

  retry(): void {
    this.retries = 0;
    this.ticketRetried = false;
    void this.connect();
  }

  destroy(): void {
    this.disposed = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.endTurn();
    this.closeSocket();
    this.dropPlayers();
  }

  // ── socket ──────────────────────────────────────────────────────────

  private setState(state: AvatarState, detail?: string): void {
    this.state = state;
    this.events.onState(state, detail);
  }

  private async connect(): Promise<void> {
    if (this.disposed || (this.ws && this.ws.readyState <= WebSocket.OPEN)) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.setState("connecting");
    let ticket: Ticket;
    try {
      ticket = await api<Ticket>("/avatar/session", { method: "POST" });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) this.setState("off");
      else this.fail(e instanceof ApiError ? e.message : "Can't reach the clinic server.");
      return;
    }
    if (this.disposed) return;
    this.avatarId = ticket.avatar_id;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}${ticket.path}?ticket=${encodeURIComponent(ticket.ticket)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (e) => {
      if (this.ws !== ws) return;
      if (typeof e.data === "string") this.onJson(e.data);
      else if (e.data instanceof ArrayBuffer) this.onBinary(new Uint8Array(e.data));
    };
    ws.onclose = (e) => {
      if (this.ws !== ws || this.disposed) return;
      this.ws = null;
      this.endTurn();
      this.sync?.stop();
      this.muse?.stop();
      this.events.onSpeaking(false);
      this.onClosed(e.code, e.reason);
    };
  }

  private onClosed(code: number, reason: string): void {
    if (code === CLOSE_IDLE) {
      this.setState("resting");
      if (this.queue.length) void this.connect();
      return;
    }
    if (code === CLOSE_BAD_TICKET && !this.ticketRetried) {
      this.ticketRetried = true;
      void this.connect();
      return;
    }
    if (code === CLOSE_REFUSED || code === CLOSE_NO_SESSION || code === CLOSE_UNAVAILABLE || code === CLOSE_BAD_TICKET) {
      this.fail(reason || "The avatar service refused the session.");
      return;
    }
    // Dropped (1006), or the service closed (1011): a few quick retries.
    if (this.retries < MAX_RETRIES) {
      const delay = 1000 * 2 ** this.retries;
      this.retries += 1;
      this.setState("connecting");
      this.retryTimer = setTimeout(() => void this.connect(), delay);
      return;
    }
    this.fail(reason || "The avatar service can't be reached.");
  }

  private fail(detail: string): void {
    this.dropPlayers();
    this.setState("unavailable", detail);
  }

  private closeSocket(): void {
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onmessage = ws.onclose = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
  }

  private send(msg: Record<string, unknown>): boolean {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }

  // ── frames ──────────────────────────────────────────────────────────

  private onJson(raw: string): void {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    switch (msg.type) {
      case "session_state": {
        this.retries = 0;
        this.ticketRetried = false;
        const renderer = msg.renderer === "synctalk" && syncTalkAvatarFor(this.avatarId) ? "synctalk" : "musetalk";
        this.usePlayer(renderer, msg.reason === "connect");
        this.setState("ready");
        this.pump();
        break;
      }
      case "synctalk":
        if (msg.msg && typeof msg.msg === "object") this.sync?.feedControl(msg.msg as Record<string, unknown>);
        break;
      case "avatar-speak-completed":
        if (performance.now() - this.sentAt < 500) break;
        this.endTurn();
        this.pump();
        break;
      case "avatar_error":
        if (typeof msg.message === "string") console.warn("[avatar]", msg.code, msg.message);
        break;
      default:
        break;
    }
  }

  private onBinary(data: Uint8Array): void {
    if (data.length > 1 && data[0] === SYNCTALK_RELAY_PREFIX) {
      this.sync?.feedBinary(data.subarray(1));
      return;
    }
    if (this.renderer === "musetalk") this.muse?.feed(data);
  }

  private usePlayer(renderer: "synctalk" | "musetalk", newSession: boolean): void {
    if (renderer === this.renderer) {
      if (renderer === "synctalk" && newSession) this.sync?.newSession();
      return;
    }
    this.dropPlayers();
    this.renderer = renderer;
    if (renderer === "synctalk") {
      const avatar = syncTalkAvatarFor(this.avatarId)!;
      const player = new SyncTalkPlayer({
        canvas: this.canvas,
        avatar,
        transport: "relay",
        assetBase: "/avatar/avatars",
        workletUrl: "/avatar/synctalk/pcm-player-processor.js",
        events: {
          onSpeakingChange: (on) => this.events.onSpeaking(on),
          onNotice: (m) => console.warn("[avatar]", m),
        },
      });
      player.start();
      this.sync = player;
    } else {
      const player = new MuseTalkPlayer(this.canvas, (on) => this.events.onSpeaking(on));
      player.start().catch((e) => console.warn("[avatar] audio could not start", e));
      this.muse = player;
    }
  }

  private dropPlayers(): void {
    this.sync?.destroy();
    this.muse?.destroy();
    this.sync = null;
    this.muse = null;
    this.renderer = null;
  }

  // ── reply queue ─────────────────────────────────────────────────────

  private pump(): void {
    if (this.inFlight || this.state !== "ready" || !this.queue.length) return;
    const text = this.queue.shift()!;
    if (!this.send({ type: "speak", text })) {
      this.queue.unshift(text);
      return;
    }
    this.inFlight = true;
    this.sentAt = performance.now();
    this.sync?.resumeAudio();
    this.muse?.resumeAudio();
    this.turnTimer = setTimeout(() => {
      this.endTurn();
      this.pump();
    }, TURN_TIMEOUT_MS);
  }

  private endTurn(): void {
    this.inFlight = false;
    if (this.turnTimer) clearTimeout(this.turnTimer);
    this.turnTimer = null;
  }
}
