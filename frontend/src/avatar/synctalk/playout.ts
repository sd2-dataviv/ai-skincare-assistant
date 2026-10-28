/**
 * Fixed-rate video playout for the SyncTalk avatar, slaved to the audio clock.
 *
 * The backend sends frames in real time with no send-ahead, so the client
 * keeps a small jitter buffer and shows one frame per `1000 / fps` ms of wall
 * clock while the audio plays:
 *
 *   prefill   hold until a few frames are queued AND audio is playing, so
 *             sound and picture start together;
 *   snap      when a clause-first frame is near the head and its audio has
 *             started, drop what is left of the previous clause and show it;
 *   drift     nudge the interval by up to 12 % to stay within half a frame of
 *             the audio position;
 *   resync    two seconds behind: drop the backlog; two seconds ahead: wait;
 *   underrun  hold the last frame, never run ahead.
 *
 * Pure: the caller owns the queue and the decoding, and applies the returned
 * step. Extracted from the reference renderer (realtime_avatar_updated/
 * frontend avtar.ts `_advanceFixedRate`) with its tuning unchanged.
 */

export interface PlayoutState {
  /** False until this turn's prefill has been met. */
  started: boolean;
  /** Index of the frame on screen since the turn began; -1 before the first. */
  frameIdx: number;
  /** Wall-clock milliseconds accumulated towards the next frame. */
  accumMs: number;
  /** Clause start (seconds) the video last snapped to; -1 for none. */
  lastSnappedChunkStart: number;
}

export function newPlayoutState(): PlayoutState {
  return { started: false, frameIdx: -1, accumMs: 0, lastSnappedChunkStart: -1 };
}

export interface PlayoutTick {
  dtMs: number;
  fps: number;
  /** Video buffered before playout starts, in seconds. */
  prefillS: number;
  /** Frames the video sits behind the audio position so lips are centred. */
  lipLeadFrames: number;
  /** Audio played since the turn's first chunk, in seconds. */
  audioPlayedSec: number;
  audioPlaying: boolean;
  /** A frame is still being decoded: at most one decode runs at a time. */
  decodeBusy: boolean;
  /** Queued frames, head first; `chunkStart` marks a clause-first frame. */
  queue: ReadonlyArray<{ chunkStart?: number }>;
}

/** Drop `drop` frames from the head of the queue, then show the next one. */
export interface PlayoutStep {
  drop: number;
  show: boolean;
}

const HOLD: PlayoutStep = { drop: 0, show: false };

export function prefillFrames(fps: number, prefillS: number): number {
  return Math.max(2, Math.round(fps * prefillS));
}

/** Advance the playout by one render tick, updating `s` in place. */
export function advancePlayout(s: PlayoutState, t: PlayoutTick): PlayoutStep {
  const interframe = 1000 / t.fps;
  const prefill = prefillFrames(t.fps, t.prefillS);
  const queued = t.queue.length;

  if (!s.started) {
    if (queued < prefill || !t.audioPlaying) return HOLD;
    s.started = true;
    s.accumMs = 0;
  }

  const audioIdx = t.audioPlayedSec * t.fps;

  // Clause-start snap: each clause begins tight to its own audio, which stops
  // drift from accumulating across clauses. Latched once per clause.
  if (t.audioPlaying && !t.decodeBusy && queued > 0) {
    const lookahead = Math.max(2, Math.round(t.fps * 0.2));
    for (let i = 0; i < Math.min(queued, lookahead); i++) {
      const cs = t.queue[i].chunkStart;
      if (cs == null || cs === s.lastSnappedChunkStart) continue;
      if (audioIdx >= cs * t.fps - t.lipLeadFrames) {
        s.lastSnappedChunkStart = cs;
        s.frameIdx = Math.round(audioIdx);
        s.accumMs = 0;
        return { drop: i, show: true };
      }
      break;
    }
  }

  // Positive drift: video ahead of the audio. Negative: behind.
  const drift = s.frameIdx - (audioIdx - t.lipLeadFrames);
  let interval = interframe;
  if (t.audioPlaying && Math.abs(drift) > 0.5) {
    let k = Math.max(-0.12, Math.min(0.12, drift * 0.08));
    // Speeding up consumes the buffer; below the prefill depth there is
    // nothing to speed up into, and asking only turns jitter into freezes.
    if (k < 0 && queued < prefill) k = 0;
    interval = interframe * (1 + k);
  }

  const resync = Math.max(20, t.fps * 2);
  if (drift <= -resync && !t.decodeBusy && queued > 0) {
    // Far behind (hidden tab, long stall): drop the stale frames. The index
    // advances by what was actually consumed, so if the backlog arrives a
    // tick later this branch fires again instead of declaring itself caught up.
    const skip = Math.min(queued - 1, Math.max(0, Math.floor(-drift) - 1));
    s.frameIdx += skip + 1;
    s.accumMs = 0;
    return { drop: skip, show: true };
  }
  if (drift >= resync) {
    // Far ahead of a stalled audio clock: wait for the audio.
    s.accumMs = 0;
    return HOLD;
  }

  s.accumMs += t.dtMs;
  if (s.accumMs < interval) return HOLD;
  if (queued > 0 && !t.decodeBusy) {
    s.frameIdx++;
    s.accumMs -= interval;
    if (s.accumMs > interval * 2) s.accumMs = interval; // no burst after a stall
    return { drop: 0, show: true };
  }
  s.accumMs = interval; // underrun: hold the last frame
  return HOLD;
}
