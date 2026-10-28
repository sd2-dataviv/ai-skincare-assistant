/**
 * SyncTalk avatar player: one <canvas>, one socket, one audio worklet.
 *
 * Framework-agnostic port of the reference front-end's SyncTalk path
 * (realtime_avatar_updated/frontend, unity/avtar/avtar.ts). It renders the
 * avatar the way that app's `<canvas #canvas>` does:
 *
 *   idle     drawn locally from a sprite atlas (SyncTalkIdle);
 *   talking  the backend's frames — a complete matted figure per frame,
 *            shipped "packed" (colour on top, alpha underneath) — played out
 *            at a fixed rate slaved to the audio clock (playout.ts);
 *   audio    24 kHz PCM through the reference pcm-player worklet.
 *
 * The backend is used as a renderer: `speak(text)` sends text in "direct"
 * mode and the avatar says it. It has no notion of who the user is, so this
 * class sends it no credentials and never trusts it for session state.
 *
 * Two transports:
 *   socket  this class opens the engine socket itself (a loopback engine, dev);
 *   relay   the avatar service drives the engine server-side and the session
 *           socket carries its output; the owner hands it in with
 *           feedControl()/feedBinary(). The service decides what is said, so
 *           speak() and setVoice() have nothing to send.
 *
 * Left out on purpose (present in the reference, not needed here): WebRTC
 * transport, garments and size switching, the frame-debug strip, the SAM
 * login path and its hardcoded defaults, and the diagnostic upload of
 * microphone recordings. The microphone is not captured here at all.
 */

import type { SyncTalkAvatar } from "./avatars";
import { buildSyncTalkUrl } from "./avatars";
import { isNearBlack, isPackedFrame, parseSyncTalkFrame, unpackPackedAlpha } from "./frames";
import { advancePlayout, newPlayoutState, type PlayoutState } from "./playout";
import { CLOSE_TRY_AGAIN_LATER, faceReconnectDelay } from "./reconnect";
import { SyncTalkIdle } from "./SyncTalkIdle";

/** Backing-store size of the canvas: the 512 px tiles drawn at 2x. */
export const SYNCTALK_CANVAS_SIZE = 1024;

export type SyncTalkStatus =
  /** Socket opening. */
  | "connecting"
  /** Socket open; the backend is still warming this avatar. */
  | "loading"
  | "ready"
  /** Dropped; a reconnect is scheduled. */
  | "reconnecting"
  /** Not retrying: closed for good, refused, or out of retries. */
  | "failed";

/** Why the engine gave up: at its session cap, or not answering. */
export type SyncTalkFailure = "busy" | "unreachable";

export interface SyncTalkPlayerEvents {
  /** `failure` comes with "failed" only. */
  onStatus?: (status: SyncTalkStatus, failure?: SyncTalkFailure) => void;
  /** True from a turn's first audio until its last word has been heard. */
  onSpeakingChange?: (speaking: boolean) => void;
  /** A non-fatal problem the backend reported. */
  onNotice?: (message: string) => void;
}

export interface SyncTalkPlayerOptions {
  canvas: HTMLCanvasElement;
  avatar: SyncTalkAvatar;
  /** The backend's `/ws` URL, without the avatar parameters. */
  /** Engine socket for the "socket" transport; unused when relayed. */
  wsUrl?: string;
  /** "socket" (default): open the engine socket here. "relay": frames arrive via feed*(). */
  transport?: "socket" | "relay";
  /** e.g. "/frontend/assets/avatars" */
  assetBase: string;
  /** URL of pcm-player-processor.js. */
  workletUrl: string;
  events?: SyncTalkPlayerEvents;
}

export interface SyncTalkVoiceSetting {
  lang?: string;
  gender?: string;
  speed?: number;
}

type QueuedFrame = {
  t: number;
  image: Uint8Array;
  chunkStart?: number;
  /** Set once a decode has started; `bitmap` is filled when it is "ready". */
  decode?: "decoding" | "ready" | "failed";
  bitmap?: ImageBitmap | null;
};

/** Buffered frames are bounded (12 s at 25 fps) so a stalled tab cannot grow
 *  the queue until the page runs out of memory. */
const MAX_VIDEO_QUEUE = 300;
/** Late frames of a stopped turn are dropped for this long. */
const SUPPRESS_AFTER_STOP_MS = 1500;
/** A barge for the turn being replaced must not suppress the new turn. */
const NEW_TURN_GUARD_MS = 2500;
/** One frame must decode within three frame periods or it is dropped. */
const DECODE_BUDGET_MS = 120;
/** Frames at the head of a queue are decoded this far ahead of their tick, so
 *  showing one is a pointer swap and the cadence is not at the mercy of the
 *  JPEG decode + alpha unpack (which used to start only when a frame was due,
 *  putting every frame on screen a variable few ms late — the visible judder). */
const DECODE_AHEAD = 6;
const MAX_PARALLEL_DECODES = 2;
/** No transition frame for this long means the transition is over. */
const TRANSITION_IDLE_MS = 200;
/** A turn that produced no audio for this long is given up on. */
const TURN_STALL_MS = 25000;
/** Mid-turn, with nothing left to play and no audio arriving for this long,
 *  the avatar goes back to idle instead of holding a talking frame. */
const SPEECH_STALL_MS = 10000;
/** After a reply is sent the idle glides to rest and waits there this long for
 *  the reply's audio before it breathes again. */
const TURN_SETTLE_WAIT_MS = 2500;
/** Ticks the idle takes to glide to rest once the reply's audio is in. The first
 *  talking frame (the rest pose) follows 580-610 ms later (the audio worklet's
 *  600 ms preroll, measured 2026-10-17), so a 480 ms glide lands just before it;
 *  taking over mid-glide dropped the shoulders up to 3 px at once. */
const ENTRY_SETTLE_FRAMES = 12;
/** Replies waiting their turn; beyond this the oldest is dropped. */
const MAX_QUEUED_UTTERANCES = 8;
/** A reply that arrived before the avatar was ready is spoken if readiness
 *  comes within this window; an older one would be out of context. */
const PENDING_SPEECH_TTL_MS = 20000;
/** An open socket whose avatar never becomes ready is given up on. */
const READY_TIMEOUT_MS = 45000;
const LIP_LEAD_FRAMES = 0.5;

export class SyncTalkPlayer {
  private readonly canvas: HTMLCanvasElement;
  private readonly avatar: SyncTalkAvatar;
  private readonly url: string;
  private readonly relay: boolean;
  private readonly workletUrl: string;
  private readonly events: SyncTalkPlayerEvents;
  private readonly idle: SyncTalkIdle;

  private ctx: CanvasRenderingContext2D | null = null;
  private disposed = false;
  private started = false;
  private raf = 0;

  // ── socket ────────────────────────────────────────────────────────────
  private ws: WebSocket | null = null;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private avatarReady = false;
  /** The backend has announced this avatar ready at least once. */
  private everReady = false;
  /** The backend said it is at its session cap; the close that follows is final. */
  private rejected = false;
  private readyTimer: ReturnType<typeof setTimeout> | null = null;
  private voice: SyncTalkVoiceSetting = {};

  // ── audio ─────────────────────────────────────────────────────────────
  private audioCtx: AudioContext | null = null;
  private pcmNode: AudioWorkletNode | null = null;
  /** Audio clock: the worklet's played-sample count, read about 4x a second
   *  and interpolated with wall-clock in between. */
  private apSamples = 0;
  private apSr = 24000;
  private apAt = 0;
  private apPlaying = false;
  private prevApPlaying = false;
  /** The worklet counts across the session; each turn measures from here. */
  private apBaseline = 0;

  // ── playout ───────────────────────────────────────────────────────────
  private fps = 25;
  private videoQueue: QueuedFrame[] = [];
  private transitionQueue: QueuedFrame[] = [];
  private playout: PlayoutState = newPlayoutState();
  private currentBitmap: ImageBitmap | null = null;
  /** Frames whose decode has started and whose bitmap is not yet on screen. */
  private decoding = new Set<QueuedFrame>();
  private decodesInFlight = 0;
  /** Taken off a queue to be shown, but its decode has not finished yet. */
  private pendingShow: QueuedFrame | null = null;
  private prevRenderTs = 0;
  private holdFrame = false;

  /** True while this turn's audio is in flight or still draining. */
  private speechActive = false;
  /** Latched once this turn's audio actually plays. */
  private speechStarted = false;
  private drainingToIdle = false;
  /** The backend said the turn is over (not merely a gap between clauses). */
  private drainFinal = false;
  /** The backend finished sending; "done" is held until the audio is heard. */
  private completedPending = false;
  private suppressSpeechUntil = 0;
  private newTurnGuardUntil = 0;
  private transitionInFlight = false;
  private transitionLastSeenAt = 0;
  private lastTransitionFrameAt = 0;
  private forcedRestThisTurn = false;
  private forcedRestOnExit = false;
  /** Until then a reply is on its way: the idle glides to rest and waits. */
  private settleForTurnUntil = 0;

  // ── utterances ────────────────────────────────────────────────────────
  /** Replies waiting for the current one to finish, spoken in order. */
  private utterances: string[] = [];
  private turnInFlight = false;
  private turnActivityAt = 0;
  private pendingSpeech: { text: string; at: number } | null = null;

  // ── last good frame, repainted when a tick has nothing to draw ─────────
  private holdCanvas: HTMLCanvasElement | null = null;
  private holdCtx: CanvasRenderingContext2D | null = null;
  private blackCanvas: HTMLCanvasElement | null = null;
  private blackCtx: CanvasRenderingContext2D | null = null;

  constructor(opts: SyncTalkPlayerOptions) {
    this.canvas = opts.canvas;
    this.avatar = opts.avatar;
    this.relay = opts.transport === "relay";
    this.url = opts.wsUrl ? buildSyncTalkUrl(opts.wsUrl, opts.avatar) : "";
    this.workletUrl = opts.workletUrl;
    this.events = opts.events ?? {};
    this.idle = new SyncTalkIdle(opts.assetBase, opts.avatar);
  }

  /** True while the avatar is audibly speaking (or about to). */
  get speaking(): boolean {
    return this.speechActive;
  }

  /** Mount: idle loop, audio, socket. Safe to call once per instance. */
  start(): void {
    if (this.started || this.disposed) return;
    this.started = true;

    if (this.canvas.width !== SYNCTALK_CANVAS_SIZE) this.canvas.width = SYNCTALK_CANVAS_SIZE;
    if (this.canvas.height !== SYNCTALK_CANVAS_SIZE) this.canvas.height = SYNCTALK_CANVAS_SIZE;
    // Transparent canvas: the scene behind the avatar is a CSS background.
    this.ctx = this.canvas.getContext("2d", { alpha: true, desynchronized: false });
    if (this.ctx) {
      this.ctx.imageSmoothingEnabled = true;
      this.ctx.imageSmoothingQuality = "high";
    }

    this.idle.load().catch((e) => console.warn("[synctalk] idle atlas not loaded", e));
    document.addEventListener("visibilitychange", this.onVisibilityChange);
    document.addEventListener("pointerdown", this.unlockAudio);
    document.addEventListener("keydown", this.unlockAudio);
    this.raf = requestAnimationFrame(this.renderLoop);

    // Audio first, so no audio frame can arrive before the worklet exists.
    this.initAudio()
      .catch((e) => {
        console.warn("[synctalk] audio init failed", e);
        this.events.onNotice?.("The avatar's audio could not be started.");
      })
      .finally(() => {
        if (this.disposed) return;
        // Relayed: the service has the engine; readiness arrives via feedControl()
        // — often before the audio is up, so never step back from "ready".
        if (this.relay) {
          if (!this.avatarReady) this.setStatus("loading");
        } else this.openSocket();
      });
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.clearReadyTimer();
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
    document.removeEventListener("pointerdown", this.unlockAudio);
    document.removeEventListener("keydown", this.unlockAudio);

    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }

    try {
      this.pcmNode?.port.postMessage({ type: "STOP" });
      this.pcmNode?.disconnect();
    } catch {
      /* context already closed */
    }
    this.pcmNode = null;
    const ac = this.audioCtx;
    this.audioCtx = null;
    if (ac && ac.state !== "closed") ac.close().catch(() => {});

    this.dropBitmap();
    for (const f of this.decoding) f.bitmap?.close(); // in-flight ones close on arrival
    this.decoding.clear();
    this.pendingShow = null;
    this.videoQueue = [];
    this.transitionQueue = [];
    this.utterances = [];
    this.pendingSpeech = null;
    this.idle.dispose();
    // Whoever draws on this canvas next must not inherit the last figure.
    this.ctx?.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }

  /** Resume playback audio; call from a user gesture (mic, send, stop). */
  resumeAudio(): void {
    const ac = this.audioCtx;
    if (ac && ac.state === "suspended") ac.resume().catch(() => {});
  }

  /** TTS voice for the following utterances; replayed on reconnect. */
  setVoice(v: SyncTalkVoiceSetting): void {
    this.voice = { ...this.voice, ...v };
    this.sendJson({ type: "set-voice", ...this.voice });
  }

  /**
   * Say `text`. Replies are spoken one after another: a text sent while the
   * avatar is talking would cut the current sentence off on the backend.
   */
  speak(text: string): void {
    if (this.disposed || text.trim().length < 2) return;
    if (!this.canSend()) {
      // Not ready yet (first connect, reconnect): keep only the latest.
      this.pendingSpeech = { text, at: performance.now() };
      return;
    }
    this.utterances.push(text);
    if (this.utterances.length > MAX_QUEUED_UTTERANCES) this.utterances.shift();
    this.pumpUtterances();
  }

  /** Stop talking now and forget anything queued. */
  stop(): void {
    this.utterances = [];
    this.pendingSpeech = null;
    this.turnInFlight = false;
    this.holdFrame = false;
    this.videoQueue.length = 0;
    this.suppressSpeechUntil = performance.now() + SUPPRESS_AFTER_STOP_MS;
    this.drainingToIdle = false;
    this.settleForTurnUntil = 0;
    this.resetRenderClock();
    this.postToWorklet({ type: "STOP" });
    this.sendJson({ type: "mode", value: "stop" });
  }

  // ── utterance queue ───────────────────────────────────────────────────

  private canSend(): boolean {
    return this.avatarReady && this.ws?.readyState === WebSocket.OPEN;
  }

  private pumpUtterances(): void {
    if (this.turnInFlight || !this.canSend()) return;
    const text = this.utterances.shift();
    if (text === undefined) return;
    const now = performance.now();
    // A new turn: frames from it must not be taken for a stopped turn's tail.
    this.suppressSpeechUntil = 0;
    this.newTurnGuardUntil = now + NEW_TURN_GUARD_MS;
    this.turnInFlight = true;
    this.turnActivityAt = now;
    this.beginTurnSettle();
    // Direct mode speaks the text as given. `{query}` is the one envelope the
    // backend reads as text whatever the text itself looks like.
    this.sendJson({ type: "mode", value: "direct" });
    this.sendJson({ query: text });
  }

  /**
   * A reply is on its way. The idle keeps breathing: it glides to rest when the
   * reply's audio lands (ENTRY_SETTLE_FRAMES, ~600 ms before the first talking
   * frame). Gliding here parked the figure motionless for the 0.8-1.3 s the
   * reply takes to arrive — a frozen avatar before every answer.
   */
  private beginTurnSettle(): void {
    if (this.speechActive) return;
    this.settleForTurnUntil = performance.now() + TURN_SETTLE_WAIT_MS;
  }

  private finishTurn(): void {
    if (!this.turnInFlight) return;
    this.turnInFlight = false;
    this.pumpUtterances();
  }

  // ── socket ────────────────────────────────────────────────────────────

  private setStatus(status: SyncTalkStatus, failure?: SyncTalkFailure): void {
    this.events.onStatus?.(status, failure);
  }

  private clearReadyTimer(): void {
    if (this.readyTimer) clearTimeout(this.readyTimer);
    this.readyTimer = null;
  }

  private sendJson(msg: Record<string, unknown>): boolean {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify(msg));
      return true;
    } catch {
      return false;
    }
  }

  private openSocket(): void {
    if (this.disposed) return;
    this.reconnectTimer = null;
    this.avatarReady = false;
    this.setStatus(this.reconnectAttempt > 0 ? "reconnecting" : "connecting");

    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      console.warn("[synctalk] socket could not be opened", e);
      this.setStatus("failed", "unreachable");
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      // Open is not ready: the backend announces `avatar-ready` once warm.
      this.setStatus("loading");
      this.clearReadyTimer();
      this.readyTimer = setTimeout(() => {
        if (this.ws !== ws || this.avatarReady || this.disposed) return;
        console.warn("[synctalk] avatar not ready in time; giving up");
        ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
        this.ws = null;
        try {
          ws.close();
        } catch {
          /* already closed */
        }
        this.setStatus("failed", "unreachable");
      }, READY_TIMEOUT_MS);
      if (Object.keys(this.voice).length) this.sendJson({ type: "set-voice", ...this.voice });
      // Idle is drawn here, so the backend need not encode and send it.
      this.sendJson({
        type: "client-caps",
        localIdle: true,
        bargeTransitionFrames: this.avatar.variant === "sync",
      });
    };
    ws.onmessage = (e) => {
      if (this.ws !== ws) return;
      if (typeof e.data === "string") this.handleControl(e.data);
      else if (e.data instanceof ArrayBuffer) this.handleBinary(new Uint8Array(e.data));
    };
    ws.onerror = () => {
      // onclose follows and decides what happens next.
    };
    ws.onclose = (e) => {
      if (this.ws !== ws || this.disposed) return;
      this.ws = null;
      this.avatarReady = false;
      this.clearReadyTimer();
      // Nothing more will arrive: go back to idle rather than freeze on the
      // last talking frame.
      this.videoQueue.length = 0;
      this.utterances = [];
      this.turnInFlight = false;
      this.resetRenderClock();
      this.postToWorklet({ type: "STOP" });

      const busy = this.rejected || e.code === CLOSE_TRY_AGAIN_LATER;
      const delay = busy
        ? null
        : faceReconnectDelay(e.code, this.reconnectAttempt, this.everReady);
      if (delay === null) {
        this.setStatus("failed", busy ? "busy" : "unreachable");
        return;
      }
      this.reconnectAttempt += 1;
      this.setStatus("reconnecting");
      this.reconnectTimer = setTimeout(() => this.openSocket(), delay);
    };
  }

  // ── relay transport ───────────────────────────────────────────────────

  /** An engine control message the session socket carried. */
  feedControl(msg: Record<string, unknown>): void {
    if (this.disposed || !msg || typeof msg !== "object") return;
    this.handleControlMessage(msg);
  }

  /** An engine binary frame the session socket carried (relay prefix removed). */
  feedBinary(data: Uint8Array): void {
    if (this.disposed || data.length === 0) return;
    this.handleBinary(data);
  }

  /** The session socket reconnected: the engine session behind it is a new one. */
  newSession(): void {
    if (this.disposed) return;
    this.utterances = [];
    this.pendingSpeech = null;
    this.turnInFlight = false;
    this.holdFrame = false;
    this.videoQueue.length = 0;
    this.transitionQueue.length = 0;
    this.transitionInFlight = false;
    this.completedPending = false;
    this.suppressSpeechUntil = 0;
    this.settleForTurnUntil = 0;
    this.resetRenderClock();
    this.postToWorklet({ type: "STOP" });
    this.avatarReady = false;
    this.setStatus("loading");
  }

  private handleControl(raw: string): void {
    let json: Record<string, unknown>;
    try {
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return;
      json = parsed as Record<string, unknown>;
    } catch {
      return;
    }
    this.handleControlMessage(json);
  }

  private handleControlMessage(json: Record<string, unknown>): void {
    const type = json.type;

    if (type === "avatar-ready") {
      this.avatarReady = true;
      this.everReady = true;
      this.reconnectAttempt = 0;
      this.clearReadyTimer();
      this.setStatus("ready");
      const pending = this.pendingSpeech;
      this.pendingSpeech = null;
      if (pending && performance.now() - pending.at < PENDING_SPEECH_TTL_MS) {
        this.utterances.push(pending.text);
      }
      this.pumpUtterances();
    } else if (type === "avatar-loading") {
      this.avatarReady = false;
      this.setStatus("loading");
    } else if (type === "rejected") {
      // Sent just before a 1013 close: the backend is at its session cap.
      this.rejected = true;
    } else if (type === "frame-rate-hint") {
      const fps = json.fps;
      if (typeof fps === "number" && fps > 0 && fps <= 60) this.fps = fps;
    } else if (type === "completed" || type === "avatar-speak-completed") {
      const barge = json.barge === true || json.reason === "interrupted";
      this.handleSpeakCompleted(barge);
    } else if (type === "speech-draining") {
      // Sent on every gap between clauses; only `final` means the turn is over.
      if (this.speechActive) {
        this.drainingToIdle = true;
        this.drainFinal = json.final === true;
      }
    } else if (type === "user-input-received") {
      this.suppressSpeechUntil = 0;
    } else if (type === "turn-start") {
      // Relayed: the service has just sent the engine a new reply to speak.
      // Its frames are not the tail of a stopped turn.
      this.suppressSpeechUntil = 0;
      this.newTurnGuardUntil = performance.now() + NEW_TURN_GUARD_MS;
      this.turnActivityAt = performance.now();
      this.beginTurnSettle();
    } else if (type === "audio_control") {
      if (json.action === "pause") {
        this.holdFrame = true;
        this.postToWorklet({ type: "FLUSH" });
      } else if (json.action === "resume") {
        this.holdFrame = false;
        this.resumeAudio();
      } else if (json.action === "stop") {
        // Sent at the start of every turn and on every stop: flush.
        this.holdFrame = false;
        this.videoQueue.length = 0;
        this.resetRenderClock();
        this.postToWorklet({ type: "STOP" });
      }
    } else if (type === "error") {
      const code = typeof json.code === "string" ? json.code : "";
      console.warn("[synctalk] backend error", code, json.message);
      if (code === "pipeline_stuck" || code === "task_crashed" || code === "task_failed") {
        this.events.onNotice?.("The avatar could not speak that reply.");
        this.completedPending = false;
        this.finishTurn();
      }
    }
    // Everything else (session-config, voice-set, status, speech-prep, speed,
    // health_status, …) carries nothing this client acts on.
  }

  private handleBinary(data: Uint8Array): void {
    const frame = parseSyncTalkFrame(data);
    const now = performance.now();

    if (frame.kind === "video") {
      // Frames the backend was still sending when the turn was stopped.
      if (now < this.suppressSpeechUntil) return;
      this.videoQueue.push({ t: frame.t, image: frame.image, chunkStart: frame.chunkStart });
      if (this.videoQueue.length > MAX_VIDEO_QUEUE) {
        this.videoQueue.splice(0, this.videoQueue.length - MAX_VIDEO_QUEUE);
      }
    } else if (frame.kind === "transition") {
      this.transitionQueue.push({ t: frame.t, image: frame.image });
      if (this.transitionQueue.length > MAX_VIDEO_QUEUE) this.transitionQueue.shift();
      this.transitionInFlight = true;
      this.transitionLastSeenAt = now;
    } else if (frame.kind === "audio") {
      if (now < this.suppressSpeechUntil) return;
      // Frames are played against the audio clock. With no audio output there
      // is no clock, so the avatar stays idle instead of freezing mid-turn.
      if (!this.pcmNode) return;
      this.turnActivityAt = now;
      if (!this.speechActive) {
        // First audio of a turn. If the previous turn's "done" was still
        // held, release it now so it is never swallowed.
        this.fireDeferredCompleted();
        this.videoQueue.length = 0;
        this.playout = newPlayoutState();
        this.apBaseline = this.apSamples;
        this.speechStarted = false;
        this.forcedRestThisTurn = false;
        this.settleForTurnUntil = 0;
        this.speechActive = true;
        this.events.onSpeakingChange?.(true);
      }
      this.postToWorklet(frame.pcm);
    } else if (frame.kind === "stop") {
      this.postToWorklet({ type: "STOP" });
      this.handleSpeakCompleted(true);
    }
  }

  // ── turn end ──────────────────────────────────────────────────────────

  private handleSpeakCompleted(barge: boolean): void {
    if (barge) {
      // Interrupted: drop the buffered audio and video and go idle now. When a
      // new turn has just been sent, this notice is for the turn it replaced:
      // it must neither suppress the new turn's frames nor count as its end.
      const replaced = performance.now() <= this.newTurnGuardUntil;
      if (!replaced) this.suppressSpeechUntil = performance.now() + SUPPRESS_AFTER_STOP_MS;
      this.completedPending = !replaced;
      this.drainingToIdle = false;
      this.videoQueue.length = 0;
      this.resetRenderClock();
      this.postToWorklet({ type: "STOP" });
      return;
    }
    this.completedPending = true;
    if (!this.speechActive) {
      // Nothing is playing (the tail already drained, or the turn produced no
      // audio): there is no last word to wait for.
      this.fireDeferredCompleted();
      return;
    }
    // Normal end: the backend is done sending, but the client still holds
    // buffered audio and frames. The render loop ends the turn once the
    // audio has actually been heard.
    this.drainFinal = true;
    this.drainingToIdle = true;
  }

  /** Report the turn done, at most once per turn. */
  private fireDeferredCompleted(): void {
    if (!this.completedPending) return;
    this.completedPending = false;
    this.finishTurn();
  }

  /** Reset per-turn render state; any held "done" is released first. */
  private resetRenderClock(): void {
    this.fireDeferredCompleted();
    this.playout = newPlayoutState();
    this.apBaseline = 0; // the worklet's count is zeroed by STOP/FLUSH too
    this.apPlaying = false;
    this.speechStarted = false;
    this.forcedRestThisTurn = false;
    this.drainingToIdle = false;
    if (this.speechActive) {
      this.speechActive = false;
      this.events.onSpeakingChange?.(false);
    }
  }

  // ── audio ─────────────────────────────────────────────────────────────

  private postToWorklet(msg: Int16Array | { type: string }): void {
    try {
      this.pcmNode?.port.postMessage(msg);
    } catch {
      /* worklet gone (context closed) */
    }
  }

  private unlockAudio = (): void => this.resumeAudio();

  private async initAudio(): Promise<void> {
    // 24 kHz: the rate of the backend's TTS audio and of the worklet.
    const ac = new AudioContext({ sampleRate: 24000 });
    this.audioCtx = ac;
    await ac.audioWorklet.addModule(this.workletUrl);
    if (this.disposed || ac.state === "closed") return;

    const node = new AudioWorkletNode(ac, "pcm-player-processor", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    this.pcmNode = node;
    node.port.onmessage = (e: MessageEvent) => {
      const d = e.data;
      if (!d || (d.type !== "playing" && d.type !== "buffer-stats")) return;
      if (typeof d.playedSamples === "number") {
        this.apSamples = d.playedSamples;
        this.apSr = d.sr || this.apSr;
        this.apAt = performance.now();
        this.apPlaying = !!d.playing;
      }
      if (d.type === "buffer-stats") {
        // The backend paces its renderer off the client's real buffer depth.
        this.sendJson({ type: "buffer-state", ms: d.bufferedMs, underruns: d.underruns });
      }
    };

    // Output chain of the reference player, unchanged: rumble and mud out,
    // presence in, sibilance tamed, then a soft compressor and a limiter.
    const biquad = (type: BiquadFilterType, freq: number, q?: number, gain?: number) => {
      const f = ac.createBiquadFilter();
      f.type = type;
      f.frequency.value = freq;
      if (q !== undefined) f.Q.value = q;
      if (gain !== undefined) f.gain.value = gain;
      return f;
    };
    const softComp = ac.createDynamicsCompressor();
    softComp.threshold.value = -18;
    softComp.knee.value = 10;
    softComp.ratio.value = 2.5;
    softComp.attack.value = 0.01;
    softComp.release.value = 0.12;
    const limiter = ac.createDynamicsCompressor();
    limiter.threshold.value = -3;
    limiter.knee.value = 6;
    limiter.ratio.value = 12;
    limiter.attack.value = 0.001;
    limiter.release.value = 0.05;
    const master = ac.createGain();
    master.gain.value = 0.65; // quieter playback also leaves less echo for the mic

    const chain: AudioNode[] = [
      node,
      biquad("highpass", 30, 0.707),
      biquad("lowshelf", 200, undefined, -1.5),
      biquad("peaking", 2500, 1.1, 1),
      biquad("highshelf", 4000, undefined, 1),
      biquad("peaking", 7000, 2, -4),
      biquad("lowpass", 11000, 0.707),
      softComp,
      limiter,
      master,
      ac.destination,
    ];
    for (let i = 0; i < chain.length - 1; i++) chain[i].connect(chain[i + 1]);

    // Created outside a user gesture on some paths; harmless if running.
    this.resumeAudio();
  }

  /** Seconds of this turn's audio actually played. */
  private audioPlayedSec(): number {
    let s = (this.apSamples - this.apBaseline) / this.apSr;
    if (this.apPlaying) s += (performance.now() - this.apAt) / 1000;
    // What is heard lags what the worklet has consumed by the output path
    // (~10-40 ms on speakers, 150-250 ms on Bluetooth); the lips led the voice.
    const ac = this.audioCtx;
    if (ac) s -= (ac.outputLatency || 0) + (ac.baseLatency || 0);
    return s < 0 ? 0 : s;
  }

  private onVisibilityChange = (): void => {
    const ac = this.audioCtx;
    if (!ac) return;
    if (document.hidden) {
      // The render loop is throttled while hidden; without this the audio
      // would run on alone and the video would be seconds behind on return.
      if (this.speechActive && ac.state === "running") ac.suspend().catch(() => {});
      return;
    }
    this.resumeAudio();
    // The clock interpolates from the last worklet report; after a hidden
    // spell that report is stale and would overshoot by the whole absence.
    this.apAt = performance.now();
  };

  // ── rendering ─────────────────────────────────────────────────────────

  private dropBitmap(): void {
    try {
      this.currentBitmap?.close();
    } catch {
      /* already closed */
    }
    this.currentBitmap = null;
  }

  private startDecode(f: QueuedFrame): void {
    f.decode = "decoding";
    this.decoding.add(f);
    this.decodesInFlight++;
    void decodeFrameImage(f.image, DECODE_BUDGET_MS).then((bitmap) => {
      this.decodesInFlight--;
      if (this.disposed) {
        bitmap?.close();
        return;
      }
      f.bitmap = bitmap;
      f.decode = bitmap ? "ready" : "failed";
    });
  }

  /** Keep the next few frames of each queue decoded ahead of their tick. */
  private prefetchDecodes(): void {
    for (const q of [this.transitionQueue, this.videoQueue]) {
      const n = Math.min(q.length, DECODE_AHEAD);
      for (let i = 0; i < n; i++) {
        if (this.decodesInFlight >= MAX_PARALLEL_DECODES) return;
        if (!q[i].decode) this.startDecode(q[i]);
      }
    }
  }

  /** Put `f` on screen: immediately if decoded, else as soon as it is. */
  private showFrame(f: QueuedFrame): void {
    if (f.decode === "ready" && f.bitmap) {
      const bitmap = f.bitmap;
      f.bitmap = null;
      this.decoding.delete(f);
      this.pendingShow = null;
      this.dropBitmap();
      this.currentBitmap = bitmap;
      return;
    }
    if (f.decode === "failed") {
      // Keep the previous frame up rather than blank one.
      this.decoding.delete(f);
      this.pendingShow = null;
      return;
    }
    this.pendingShow = f;
    if (!f.decode) this.startDecode(f);
  }

  /** Close the bitmaps of decoded frames that were dropped from the queues
   *  (stop, barge, resync) before they were shown. */
  private releaseDiscardedDecodes(): void {
    for (const f of this.decoding) {
      if (f.decode === "decoding" || f === this.pendingShow) continue;
      if (this.videoQueue.includes(f) || this.transitionQueue.includes(f)) continue;
      f.bitmap?.close();
      f.bitmap = null;
      this.decoding.delete(f);
    }
  }

  private captureHold(w: number, h: number): void {
    if (!this.holdCanvas) {
      this.holdCanvas = document.createElement("canvas");
      this.holdCtx = this.holdCanvas.getContext("2d");
    }
    if (this.holdCanvas.width !== w) this.holdCanvas.width = w;
    if (this.holdCanvas.height !== h) this.holdCanvas.height = h;
    if (this.holdCtx) {
      this.holdCtx.clearRect(0, 0, w, h);
      this.holdCtx.drawImage(this.canvas, 0, 0);
    }
  }

  private paintHold(ctx: CanvasRenderingContext2D, w: number, h: number): void {
    if (this.holdCanvas) ctx.drawImage(this.holdCanvas, 0, 0, w, h);
  }

  private bitmapIsBlack(bm: ImageBitmap): boolean {
    if (!this.blackCanvas) {
      this.blackCanvas = document.createElement("canvas");
      this.blackCanvas.width = 24;
      this.blackCanvas.height = 24;
      this.blackCtx = this.blackCanvas.getContext("2d", { willReadFrequently: true });
    }
    const cx = this.blackCtx;
    if (!cx) return false;
    cx.clearRect(0, 0, 24, 24);
    cx.drawImage(bm, 0, 0, 24, 24);
    return isNearBlack(cx.getImageData(0, 0, 24, 24).data);
  }

  private renderLoop = (): void => {
    if (this.disposed) return;
    // Paused: keep whatever is on the canvas.
    if (!this.holdFrame) this.renderTick();
    this.raf = requestAnimationFrame(this.renderLoop);
  };

  private renderTick(): void {
    const ctx = this.ctx;
    if (!ctx) return;
    const now = performance.now();
    const dt = this.prevRenderTs ? now - this.prevRenderTs : 16;
    this.prevRenderTs = now;

    // A frame taken off the queue last tick whose decode has now finished.
    if (this.pendingShow) {
      if (!this.speechActive && !this.transitionInFlight) {
        this.pendingShow = null; // its turn is over; the sweep below frees it
      } else if (this.pendingShow.decode !== "decoding") {
        this.showFrame(this.pendingShow);
      }
    }
    this.releaseDiscardedDecodes();
    this.prefetchDecodes();
    const decodeBusy = this.pendingShow !== null;

    // Transition frames carry no audio, so they are paced by wall clock.
    if (this.transitionInFlight) {
      if (
        now - this.transitionLastSeenAt > TRANSITION_IDLE_MS &&
        this.transitionQueue.length === 0
      ) {
        this.transitionInFlight = false;
      } else if (
        this.transitionQueue.length > 0 &&
        !decodeBusy &&
        now - this.lastTransitionFrameAt >= 1000 / this.fps
      ) {
        this.showFrame(this.transitionQueue.shift()!);
        this.lastTransitionFrameAt = now;
      }
    }

    if (this.speechActive && this.apPlaying) this.speechStarted = true;
    if (this.apPlaying && !this.prevApPlaying) {
      // Audio resumed (next clause): that gap was not the end of the turn.
      this.drainingToIdle = false;
      this.drainFinal = false;
    }
    this.prevApPlaying = this.apPlaying;

    // Hand-offs happen on the idle loop's rest frame, once each way.
    const entryReady = this.speechActive && this.speechStarted && !!this.currentBitmap;
    if (entryReady && !this.forcedRestThisTurn) {
      this.idle.forceRestPhase();
      this.forcedRestThisTurn = true;
    }

    if (this.speechActive) {
      // The turn ends when the backend has said it is over AND the last frame
      // has been shown AND the audio has stopped. A gap between clauses has
      // no such signal, so the last talking frame is held through it — but
      // not for ever: when no audio has arrived or played for a while (the
      // backend went quiet, or the browser is not playing audio) it ends too.
      const drained = !this.apPlaying && this.videoQueue.length === 0;
      const stalled = !this.apPlaying && now - this.turnActivityAt > SPEECH_STALL_MS;
      if (stalled) {
        this.completedPending = true;
        this.videoQueue.length = 0;
      }
      if (
        (this.drainingToIdle && this.drainFinal && drained && !this.transitionInFlight) ||
        stalled
      ) {
        this.speechActive = false;
        this.drainingToIdle = false;
        this.lastTransitionFrameAt = 0;
        this.events.onSpeakingChange?.(false);
        this.fireDeferredCompleted();
      } else if (!this.transitionInFlight) {
        const step = advancePlayout(this.playout, {
          dtMs: dt,
          fps: this.fps,
          prefillS: this.avatar.prefillS,
          lipLeadFrames: LIP_LEAD_FRAMES,
          audioPlayedSec: this.audioPlayedSec(),
          audioPlaying: this.apPlaying,
          decodeBusy: this.pendingShow !== null,
          queue: this.videoQueue,
        });
        if (step.drop > 0) this.videoQueue.splice(0, step.drop);
        if (step.show) {
          const next = this.videoQueue.shift();
          if (next) this.showFrame(next);
        }
      }
    } else if (this.videoQueue.length) {
      // Not speaking: frames have nothing to be played against.
      this.videoQueue.length = 0;
    }

    // A turn in flight that has gone silent is not coming back.
    if (this.turnInFlight && !this.speechActive && now - this.turnActivityAt > TURN_STALL_MS) {
      this.completedPending = false;
      this.finishTurn();
    }

    // The talking frame is dropped the tick speech ends: no frame ever shows
    // both the idle figure and the talking one.
    if (!this.speechActive && !this.transitionInFlight && this.currentBitmap) this.dropBitmap();

    // Exit hand-off, decided after the turn-end checks above so the tick speech
    // ends already draws the rest frame. Decided before them, that one tick drew
    // whatever breath the idle loop had run to during the reply — up to 4 px of
    // shoulder lift, then back down a tick later (the shoulder "double jump").
    const showSpeech =
      (this.speechActive && this.speechStarted && !!this.currentBitmap) || this.transitionInFlight;
    if (!showSpeech && !this.forcedRestOnExit) {
      this.idle.forceRestPhase();
      this.forcedRestOnExit = true;
    } else if (showSpeech) {
      this.forcedRestOnExit = false;
    }

    const w = this.canvas.width;
    const h = this.canvas.height;
    if (this.idle.ready) {
      ctx.clearRect(0, 0, w, h);
      ctx.globalAlpha = 1;
      if (!this.speechActive) {
        // A turn that ended before its first frame (or whose audio never came)
        // leaves the idle parked at rest; one still on its way keeps gliding.
        if (this.idle.settling && now >= this.settleForTurnUntil) this.idle.forceRestPhase();
        if (this.idle.draw(ctx, w, h)) this.captureHold(w, h);
        else if (!this.currentBitmap) this.paintHold(ctx, w, h);
      } else if (!this.currentBitmap) {
        // Talking, but no frame decoded yet (audio preroll): be on the rest
        // frame, the engine's first frame of the turn, before that frame shows.
        this.idle.settleToRest(ENTRY_SETTLE_FRAMES);
        if (this.idle.draw(ctx, w, h)) this.captureHold(w, h);
        else this.paintHold(ctx, w, h);
      }
    }
    if (this.currentBitmap) {
      if (this.drainingToIdle && this.bitmapIsBlack(this.currentBitmap)) {
        this.paintHold(ctx, w, h);
      } else {
        ctx.drawImage(this.currentBitmap, 0, 0, w, h);
      }
      this.captureHold(w, h);
    }
  }
}

/**
 * Decode one engine frame into a straight-alpha bitmap. Resolves null if the
 * decode fails or misses `budgetMs` (a late bitmap is closed when it lands), so
 * a stuck decode can never freeze the playout.
 */
function decodeFrameImage(image: Uint8Array, budgetMs: number): Promise<ImageBitmap | null> {
  return new Promise((resolve) => {
    let settled = false;
    const watchdog = setTimeout(() => {
      settled = true;
      resolve(null);
    }, budgetMs);
    (async () => {
      const bitmap = await createImageBitmap(new Blob([image as BlobPart]));
      return isPackedFrame(bitmap.width, bitmap.height) ? unpackBitmap(bitmap) : bitmap;
    })().then(
      (bitmap) => {
        if (settled) {
          bitmap.close();
          return;
        }
        settled = true;
        clearTimeout(watchdog);
        resolve(bitmap);
      },
      (e) => {
        console.warn("[synctalk] frame decode failed", e);
        if (settled) return;
        settled = true;
        clearTimeout(watchdog);
        resolve(null);
      },
    );
  });
}

/** Split a packed frame (colour over alpha) into one straight-alpha bitmap. */
async function unpackBitmap(bitmap: ImageBitmap): Promise<ImageBitmap> {
  const w = bitmap.width;
  const packedH = bitmap.height;
  const h = packedH >> 1;
  // A canvas per call: decodes can overlap after a watchdog release, and a
  // shared canvas would let two frames corrupt each other.
  const offscreen = typeof OffscreenCanvas !== "undefined";
  const cv: OffscreenCanvas | HTMLCanvasElement = offscreen
    ? new OffscreenCanvas(w, packedH)
    : document.createElement("canvas");
  if (!offscreen) {
    cv.width = w;
    cv.height = packedH;
  }
  const cx = cv.getContext("2d", { willReadFrequently: true }) as
    | OffscreenCanvasRenderingContext2D
    | CanvasRenderingContext2D
    | null;
  if (!cx) return bitmap;
  cx.drawImage(bitmap, 0, 0);
  const packed = cx.getImageData(0, 0, w, packedH).data;
  const out = new ImageData(unpackPackedAlpha(packed, w, packedH) as Uint8ClampedArray, w, h);
  cv.width = w;
  cv.height = h;
  cx.putImageData(out, 0, 0);
  const result =
    cv instanceof HTMLCanvasElement ? await createImageBitmap(cv) : cv.transferToImageBitmap();
  bitmap.close();
  return result;
}
