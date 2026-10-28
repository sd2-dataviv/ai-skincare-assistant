/**
 * The avatar service's own face ("musetalk" renderer): what the service sends when its SyncTalk engine is busy or
 * down. Same wire format as the AI Ecosystem web app (components/AiAgent/AiAvatar.tsx):
 *
 *   0x01  video frame: float32 (big-endian) presentation time in seconds, then a JPEG
 *   0x02  audio: 16-bit mono PCM at 22 050 Hz
 *   0x03  sync: float32 offset of the video clock against the audio clock
 *   0x12  stop: drop what is buffered (end of an utterance, or interrupted)
 *
 * A frame may carry four zero bytes of padding before its type byte.
 */

const SAMPLE_RATE = 22050;
/** ~10 s at 30 fps: a stalled tab must not grow the queue without bound. */
const MAX_VIDEO_QUEUE = 300;
/** No audio for this long = the avatar has stopped talking. */
const QUIET_MS = 600;

const PCM_PLAYER_SRC = `
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(0);
    this.readIndex = 0;
    this.port.onmessage = (e) => {
      const msg = e.data;
      if (msg && msg.type === "STOP") { this.buffer = new Float32Array(0); this.readIndex = 0; return; }
      const f32 = new Float32Array(msg.length);
      for (let i = 0; i < msg.length; i++) f32[i] = msg[i] / 32768;
      const rest = this.buffer.subarray(this.readIndex);
      const next = new Float32Array(rest.length + f32.length);
      next.set(rest, 0); next.set(f32, rest.length);
      this.buffer = next; this.readIndex = 0;
    };
  }
  process(_, outputs) {
    const out = outputs[0][0];
    if (this.buffer.length - this.readIndex >= out.length) {
      out.set(this.buffer.subarray(this.readIndex, this.readIndex + out.length));
      this.readIndex += out.length;
    } else out.fill(0);
    return true;
  }
}
registerProcessor("musetalk-pcm-player", PcmPlayer);
`;

type Frame = { t: number; jpeg: Uint8Array };

export class MuseTalkPlayer {
  private readonly canvas: HTMLCanvasElement;
  private readonly onSpeakingChange: (speaking: boolean) => void;
  private ctx2d: CanvasRenderingContext2D | null = null;
  private audio: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private queue: Frame[] = [];
  private audioStart: number | null = null;
  private videoOffset = 0;
  private synced = false;
  private raf = 0;
  private disposed = false;
  private speaking = false;
  private lastAudioAt = 0;
  private drawing = false;

  constructor(canvas: HTMLCanvasElement, onSpeakingChange: (speaking: boolean) => void) {
    this.canvas = canvas;
    this.onSpeakingChange = onSpeakingChange;
  }

  async start(): Promise<void> {
    this.ctx2d = this.canvas.getContext("2d");
    this.raf = requestAnimationFrame(this.loop);
    document.addEventListener("pointerdown", this.resumeAudio);
    document.addEventListener("keydown", this.resumeAudio);
    const ac = new AudioContext({ sampleRate: SAMPLE_RATE });
    const url = URL.createObjectURL(new Blob([PCM_PLAYER_SRC], { type: "application/javascript" }));
    try {
      await ac.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    if (this.disposed) {
      ac.close().catch(() => {});
      return;
    }
    const node = new AudioWorkletNode(ac, "musetalk-pcm-player", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
    node.connect(ac.destination);
    this.audio = ac;
    this.node = node;
  }

  resumeAudio = (): void => {
    if (this.audio?.state === "suspended") this.audio.resume().catch(() => {});
  };

  feed(data: Uint8Array): void {
    if (this.disposed) return;
    const off = data.length > 4 && data[0] === 0 && data[1] === 0 && data[2] === 0 && data[3] === 0 ? 4 : 0;
    const type = data[off];
    const payload = data.subarray(off + 1);
    if (type === 0x01) {
      if (payload.length < 5) return;
      const t = new DataView(payload.buffer, payload.byteOffset, 4).getFloat32(0, false);
      this.queue.push({ t, jpeg: payload.slice(4) });
      if (this.queue.length > MAX_VIDEO_QUEUE) this.queue.splice(0, this.queue.length - MAX_VIDEO_QUEUE);
    } else if (type === 0x02) {
      if (!this.node) return;
      const even = payload.byteLength - (payload.byteLength % 2);
      const pcm = new Int16Array(payload.slice(0, even).buffer);
      this.node.port.postMessage(pcm);
      this.lastAudioAt = performance.now();
      this.setSpeaking(true);
    } else if (type === 0x03) {
      if (payload.length < 4) return;
      this.videoOffset = new DataView(payload.buffer, payload.byteOffset, 4).getFloat32(0, false);
      this.audioStart = this.audio?.currentTime ?? 0;
      this.synced = true;
    } else if (type === 0x12) {
      this.stop();
    }
  }

  stop(): void {
    this.node?.port.postMessage({ type: "STOP" });
    this.audioStart = null;
    this.videoOffset = 0;
    this.synced = false;
    this.queue = [];
    this.setSpeaking(false);
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    document.removeEventListener("pointerdown", this.resumeAudio);
    document.removeEventListener("keydown", this.resumeAudio);
    try {
      this.node?.disconnect();
    } catch {
      /* already gone */
    }
    if (this.audio && this.audio.state !== "closed") this.audio.close().catch(() => {});
    this.queue = [];
  }

  private setSpeaking(on: boolean): void {
    if (this.speaking === on) return;
    this.speaking = on;
    this.onSpeakingChange(on);
  }

  private loop = (): void => {
    if (this.disposed) return;
    const ac = this.audio;
    let due: Frame | undefined;
    if (this.synced && ac && this.audioStart !== null) {
      const now = ac.currentTime - this.audioStart + this.videoOffset;
      while (this.queue.length && this.queue[0].t <= now) due = this.queue.shift();
    } else if (this.queue.length) {
      due = this.queue.shift();
    }
    if (due) this.draw(due.jpeg);
    if (this.speaking && performance.now() - this.lastAudioAt > QUIET_MS && !this.queue.length) this.setSpeaking(false);
    this.raf = requestAnimationFrame(this.loop);
  };

  private draw(jpeg: Uint8Array): void {
    if (this.drawing || !this.ctx2d) return; // drop a frame rather than pile up decodes
    this.drawing = true;
    createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }))
      .then((bmp) => {
        if (!this.disposed && this.ctx2d) {
          if (this.canvas.width !== bmp.width || this.canvas.height !== bmp.height) {
            this.canvas.width = bmp.width;
            this.canvas.height = bmp.height;
          }
          this.ctx2d.drawImage(bmp, 0, 0);
        }
        bmp.close();
      })
      .catch(() => undefined)
      .finally(() => {
        this.drawing = false;
      });
  }
}
