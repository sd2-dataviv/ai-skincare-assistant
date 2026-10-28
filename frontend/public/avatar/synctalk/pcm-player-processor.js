// pcm-player-processor.js — AudioWorklet that plays the WS PCM16 stream.
//   FEATURES: jitter buffer (preroll/resume), smooth under-run fade, sample
//     declick + soft join-bridge, silence-aware envelope, WSOLA time-stretch,
//     buffer-stats + playing + playedSamples messages to the main thread.
//   RELATED: served at /static/pcm-player-processor.js; loaded by
//     src/app/unity/avtar/avtar.ts (addModule + STRETCH/STOP/FLUSH messages).
//   FLAGS: processorOptions.prerollMs (variant=250ms, prod=600ms); STRETCH msg.
//   FAILURE MODES: under-run → fade to silence + re-preroll; STOP/FLUSH reset
//     the audio clock. (HF high-shelf removed 2026-06-19 — was unreachable.)
//
// PCM-Player worklet with jitter buffer + amplitude-ramp anti-click.
//
// Replaces the previous version which:
//   - Played as soon as any audio arrived (zero jitter margin → frequent
//     underruns when WS frames arrived late).
//   - On underflow, emitted output.fill(0) which is a STEP function from
//     real audio to silence. That step IS a click, despite the old code's
//     "(no clicks)" comment. Pair of clicks per underrun (start + end).
//
// New behavior:
//   1. JITTER BUFFER: hold ~PREROLL_MS ms before playback starts.
//      Absorbs WS-arrival jitter without dropping samples.
//   2. SMOOTH UNDERFLOW: when buffer is empty, fade out from the last
//      emitted sample to 0 over FADE_SAMPLES samples (~5 ms @ 24 kHz),
//      then sustain silence.
//   3. SMOOTH RESUME: when buffer refills after an underrun (or at start),
//      ramp gain from 0 to 1 over FADE_SAMPLES samples.
// The combined effect: clicks/cracks at WS jitter boundaries are gone,
// at the cost of +PREROLL_MS TTFB. Tune via the constant below.

const SR_DEFAULT = 24000;                          // matches AudioContext rate
// FIX B 2026-06-05 — DEFAULT preserved for prod; variant overrides via
// processorOptions.prerollMs from the FE constructor. Variant ?variant=sync
// suffered from a ~3 s pile-up of video frames in videoQueue while the
// worklet was still in preroll. Constructor reads opts.prerollMs (default
// = this constant) → prod keeps 600 ms, variant gets 250 ms.
// 2026-09-18 (user: "audio smooth nahi, cut cut aati hai") — 600 -> 1200.
// MEASURED, browser + pure-WS: the BACKEND delivers audio at 0.972x realtime
// (steady; pure-WS capture with no browser = 0.9723x, and the BE play loop ticks at a
// median 23.0/s against its own 25/s target). The worklet consumes at exactly 1.00x, so
// the jitter buffer drains ~2.8% per second of speech and then underruns forever:
//   t= 5.4s buf 455ms u=0 | t=15.6s buf 135ms u=0 | t=20.7s buf 16ms u=5
//   t=31.0s buf   1ms u=30 | t=46.3s buf  0ms u=66   <- 66 gaps in ONE 45s turn
// Each underrun is a 5ms fade-out + rebuffer + 5ms fade-in = the audible "cut cut".
// Buffer depth only buys TIME (B ms of cushion lasts B/0.028 ms of speech): 600 -> ~21s,
// 1200 -> ~43s. The real fix is the BE loop reaching 25/s; this is mitigation and costs
// ~600ms of extra time-to-first-word. RESUME_MS was deliberately NOT raised: the renderer
// holds video while !_apPlaying, so a deeper resume threshold converts each 40ms audio gap
// into an equally long VISIBLE video freeze. Revert: 600.
// UXLAB EXP4 2026-09-18 - 1200 -> 600 (the ORIGINAL shipped value). The 1200 was set
// EARLIER TODAY as mitigation for the audio drain, BEFORE the real cause was found. That
// cause (partial lipsync audio packets each burning a full 40ms send tick) is now fixed in
// synctalk_avatar_variant.py, delivery is 1.0019x and the cushion slope is -0.5 ms/s, so
// the extra 600ms buys nothing and costs: ~600ms more time-to-first-word on EVERY turn,
// and a turn-1 pile-up (the renderer holds video while !_apPlaying, so frames stack up -
// measured fifo 29, first-bitmap T+1841ms, settle 1275ms on turn 1 vs 0-3 / 657-673ms /
// 207-247ms on turns 2-7). Mid-playback underruns are UNAFFECTED by this: once hasStarted
// is true the worklet resumes on resumeSamples (RESUME_MS=40), not on preRollSamples.
// Revert: 1200.
const PREROLL_MS = 600;                           // initial startup jitter buffer
                                                   // 2026-05-29: 400 → 700. The idle loop now covers
                                                   // the preroll VISUALLY (Phase 1), so a deeper audio
                                                   // buffer costs only audio start-latency, not a
                                                   // frozen frame — worth it to ride out the server's
                                                   // audio-delivery jitter (the remaining underruns).
                                                   // The time-stretch then grows the cushion further.
                                                   // playback. The server paces audio to the 10fps
                                                   // frame loop (≈realtime, no cushion), so the
                                                   // client buffer can only be ~PREROLL deep. Measured
                                                   // delivery jitter: ≤266ms spikes + ≤235ms ASR
                                                   // event-loop stalls. 600ms buffers past both with
                                                   // ~2x margin so the stream never underruns →
                                                   // gapless playback. Cost: +250ms to first-audio and
                                                   // a slightly larger barge-flush. Tune down if the
                                                   // startup delay feels too long.
const RESUME_MS  = 40;                             // re-buffer threshold AFTER mid-playback underrun
                                                   // 2026-07-01 (user: "at mid playback, play even if the
                                                   // buffer isn't full to avoid gaps — don't wait for it to
                                                   // fill every time"). 300 → 40: after an underrun, RESUME
                                                   // as soon as ~40ms is available (just enough for a clean
                                                   // fade-in) instead of re-buffering a full 300ms each gap.
                                                   // The 300ms re-buffer wait was ITSELF the repeated audio
                                                   // gap on SAM's bursty/gappy TTS delivery. Initial start
                                                   // still uses the deep PREROLL_MS cushion.
const FADE_MS    = 5;                              // 5 ms ramp at boundaries
// Single-sample declick: detect step jumps > THRESH between adjacent
// samples and replace the offending sample with the mean of its
// neighbors. Mirrors the server's _spectral_declick (which only runs
// within one TTS request; this catches clicks at the join between
// requests, which is where the remaining clause-boundary clicks live).
// 0.15 is the same threshold the server uses — real speech rarely
// changes by more than that in 1/24000 s at 24 kHz.
// 2026-05-28: raised 0.15 → 0.30 to match the server's _spectral_declick.
// At 0.15 this client pass re-dulled sibilance the server (now at 0.30)
// deliberately preserves — audible as muffled "s/sh/ch" + general quality
// loss. 0.30 still catches true cross-request boundary clicks (the only
// thing this client pass is responsible for) without touching sibilance.
const DECLICK_THRESH = 0.30;

// High-frequency shelf for TTS brightness — REMOVED 2026-06-19 (R8.7) as a
// provably-dead path. It was disabled on 2026-05-27 (HF_GAIN_DB = 0.0), which
// made `const HF = (HF_GAIN_DB > 0) ? _highShelfCoeffs(...) : null` ALWAYS null,
// so the `if (HF) { …biquad… }` consumer in onmessage could never execute and
// the `_hf*` biquad-state fields were dead. Per the FLAGS-bake protocol (bake
// the proven value, no flag-flipping) the entire shelf — the HF_*/HF consts,
// _highShelfCoeffs(), the `if (HF)` biquad branch, and the _hfX1/_hfX2/_hfY1/
// _hfY2 fields + their STOP/FLUSH resets — was removed. Behavior is IDENTICAL
// (the branch was unreachable). To RE-INTRODUCE the shelf (with a denoiser
// ahead of it, per the 2026-05-27 note), restore the original block from the
// backup: backups/code_20260619_210449_R8real/.../public/static/pcm-player-processor.js

class PCMPlayerProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();

    this.buffer = new Float32Array(0);
    this.readIndex = 0;

    // FIX B 2026-06-05 — accept a per-session preroll override via
    // processorOptions.prerollMs. Prod constructs the node with no
    // override → falls back to PREROLL_MS (600 ms, unchanged). Variant
    // (?variant=sync/transparent/noah) passes 250 ms to cut the visible
    // ~3 s buildup of video frames before audio playout starts.
    const _prerollMs = (options && options.processorOptions
        && typeof options.processorOptions.prerollMs === 'number'
        && options.processorOptions.prerollMs > 0)
      ? options.processorOptions.prerollMs
      : PREROLL_MS;

    // Pre-roll budget in samples. AudioWorklet's `sampleRate` global is
    // the AudioContext rate (24 kHz here).
    this.preRollSamples = Math.round(sampleRate * (_prerollMs / 1000));
    this.resumeSamples  = Math.round(sampleRate * (RESUME_MS  / 1000));
    this.fadeSamples    = Math.round(sampleRate * (FADE_MS    / 1000));

    this.playing      = false;   // false until preroll filled
    this.hasStarted   = false;   // true once first preroll completed; further underruns use resumeSamples
    this.fadeInLeft   = 0;       // samples remaining in the resume ramp
    this.lastSample   = 0;       // last emitted sample (for fade-out on underrun)
    // 2026-06-17 — silence-aware de-click: ease audio OUT into a silence run and
    // IN out of one (via jitter-buffer lookahead) so clause↔[pause] joins and
    // stream ends don't step-click. Stays pinned at 1.0 through continuous speech
    // → seamless TTS clause joins are bit-exact (NOT an always-on fade).
    this._silenceRamp = true;
    this._silThresh   = 0.004;   // |sample| below this counts as silence
    this._silGain     = 1;       // envelope gain (1=voiced, 0=silence)
    this._inSilence   = false;   // for [GAP] console logging on enter/exit
    // 2026-06-17 — JOIN BRIDGE: a continuous clause-to-clause join (no silence
    // between sub-clauses) can leave a small waveform STEP that's below
    // DECLICK_THRESH (single-sample fix skips it) yet audible as a faint click —
    // the case the silence-ramp can't see. For steps in this band, spread the
    // boundary discontinuity over ~1.5ms with a DECAYING DC correction (chunk
    // shape preserved, only the join step removed). Clean joins (step≈0) untouched.
    this._softBridge         = true;   // A/B toggle — flip to false to compare
    this._softBridgeThresh   = 0.02;
    this._softBridgeSamples  = Math.max(4, Math.round(sampleRate * 0.0015));
    // Diagnostics: emit buffer depth + underrun count to the main thread so
    // the UI can show the audio jitter-buffer state (starved vs healthy).
    this._statsTick   = 0;
    this._underruns   = 0;
    // Cumulative count of REAL audio samples emitted to the output (not
    // silence/fade). Divided by sampleRate this is the audio playback
    // clock the main thread uses to drive video-frame advancement when
    // the audio-consumption renderer is enabled. Reset to 0 on STOP/FLUSH
    // so each turn's frames index from that turn's audio position.
    this._playedSamples = 0;

    // ── Pitch-preserving time-stretch (WSOLA), flag-gated via STRETCH ──
    // _stretchFactor < 1 slows playback (0.92 = 92% speed) WITHOUT pitch
    // change, so the client consumes input slower than the realtime feed
    // and the jitter buffer grows on its own. Disabled (factor=1) by
    // default → _processStretched is never entered, default path untouched.
    this._stretchEnabled = false;
    this._stretchFactor  = 1.0;
    this._W   = 480;                 // analysis/synthesis window (20ms @24k)
    this._Hs  = 240;                 // synthesis hop (50% overlap, COLA Hann)
    this._Lov = this._W - this._Hs;  // overlap length
    this._Ha  = this._Hs;            // analysis hop (= Hs*factor; set on STRETCH)
    this._SS  = 64;                  // WSOLA search radius (± samples)
    this._hann = new Float32Array(this._W);
    for (let i = 0; i < this._W; i++) {
      this._hann[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / this._W);
    }
    this._olaTail = new Float32Array(this._Lov);  // overlap-add carry
    this._target  = null;                          // WSOLA continuation target
    this._anaOff  = 0;                             // analysis offset from readIndex
    this._outBuf  = new Float32Array(16384);       // stretched output FIFO
    this._outRead = 0;
    this._outLen  = 0;
    // (HF biquad state removed 2026-06-19 — see header note; was unreachable.)
    // Last sample of the most recently RECEIVED chunk — used by the
    // declick pass to bridge boundaries between WS chunks. Without this,
    // a clause-boundary click that straddles two chunks (last sample of
    // chunk K = -0.23, first of chunk K+1 = 0.00, second = +0.13) is
    // invisible to a within-chunk scan because the first chunk has no
    // "next" and the second chunk's i=0 has no "prev".
    this._lastSampleIn = 0;

    this.port.onmessage = (e) => {
      // Configure time-stretch. factor in (0.5, 1.0]; <1 slows playback.
      if (e.data && e.data.type === 'STRETCH') {
        const f = Number(e.data.factor);
        if (f > 0.5 && f <= 1.0) {
          this._stretchFactor  = f;
          this._stretchEnabled = f < 0.999;
          this._Ha = Math.max(1, Math.round(this._Hs * f));
        } else {
          this._stretchFactor  = 1.0;
          this._stretchEnabled = false;
        }
        this._olaTail.fill(0);
        this._target  = null;
        this._anaOff  = 0;
        this._outRead = 0;
        this._outLen  = 0;
        return;
      }
      // Handle control messages (STOP, FLUSH) in addition to PCM arrays.
      if (e.data && e.data.type === 'STOP') {
        // Clean stop: fade out from lastSample on the next tick.
        this.buffer = new Float32Array(0);
        this.readIndex = 0;
        this.playing = false;
        this._lastSampleIn = 0;   // don't bridge declick across turns
        this._playedSamples = 0;  // new turn → audio clock restarts at 0
        this._olaTail.fill(0); this._target = null; this._anaOff = 0;
        this._outRead = 0; this._outLen = 0;
        return;
      }
      if (e.data && e.data.type === 'FLUSH') {
        // Drop any pending audio (e.g. on tab-resume after long hide).
        this.buffer = new Float32Array(0);
        this.readIndex = 0;
        this.playing = false;
        this.hasStarted = false;   // treat next playback as a fresh start (full preroll)
        this.fadeInLeft = 0;
        this._lastSampleIn = 0;
        this._playedSamples = 0;  // new turn → audio clock restarts at 0
        this._olaTail.fill(0); this._target = null; this._anaOff = 0;
        this._outRead = 0; this._outLen = 0;
        return;
      }

      const pcm16 = e.data;
      const f32 = new Float32Array(pcm16.length);
      for (let i = 0; i < pcm16.length; i++) {
        f32[i] = pcm16[i] / 32768.0;
      }

      // Sample-level declick. Replaces a sample with the linear interp
      // of its neighbors when |sample - prev| > DECLICK_THRESH. First
      // sample uses _lastSampleIn (the tail of the previous chunk) as
      // its "prev" so cross-chunk clicks at clause boundaries are caught.
      if (f32.length >= 2) {
        const _step0 = Math.abs(f32[0] - this._lastSampleIn);
        if (_step0 > DECLICK_THRESH) {
          f32[0] = 0.5 * (this._lastSampleIn + f32[1]);
        } else if (this._softBridge && _step0 > this._softBridgeThresh) {
          // JOIN BRIDGE: spread the sub-DECLICK_THRESH boundary step over ~1.5ms
          // with a decaying DC correction — full step removed at i=0, →0 at RN.
          // Chunk shape preserved; clean joins (step≤thresh) skip this entirely.
          const RN = Math.min(this._softBridgeSamples, f32.length);
          const d  = f32[0] - this._lastSampleIn;
          for (let i = 0; i < RN; i++) f32[i] -= d * (1 - i / RN);
        }
        for (let i = 1; i < f32.length - 1; i++) {
          if (Math.abs(f32[i] - f32[i - 1]) > DECLICK_THRESH) {
            f32[i] = 0.5 * (f32[i - 1] + f32[i + 1]);
          }
        }
        this._lastSampleIn = f32[f32.length - 1];
      } else if (f32.length === 1) {
        this._lastSampleIn = f32[0];
      }

      // (HF shelf removed 2026-06-19 — see header note; was unreachable.)

      // Append-only — same as before — but compact the buffer first so
      // it doesn't grow unboundedly across long sessions.
      const remaining = this.buffer.subarray(this.readIndex);
      this.buffer = new Float32Array(remaining.length + f32.length);
      this.buffer.set(remaining, 0);
      this.buffer.set(f32, remaining.length);
      this.readIndex = 0;
    };
  }

  process(_, outputs) {
    const output = outputs[0][0]; // mono
    const N      = output.length;
    if (this._stretchEnabled) return this._processStretched(output, N);
    const avail  = this.buffer.length - this.readIndex;

    // Periodic buffer-state report (~every 256ms) for the UI queue indicator.
    if ((++this._statsTick % 48) === 0) {
      this.port.postMessage({
        type: 'buffer-stats',
        bufferedMs: Math.round((avail / sampleRate) * 1000),
        underruns:  this._underruns,
        playing:    this.playing,
        playedSamples: this._playedSamples,
        sr:         sampleRate,
      });
    }

    // ── PRE-ROLL: hold quiet until we have enough margin ────────────────
    // First start uses PREROLL_MS (80ms) to absorb startup WS jitter.
    // After a mid-playback underrun, use RESUME_MS (10ms) — just enough
    // to cleanly run the fade-in; otherwise every brief jitter event
    // would cause an 80ms audible gap.
    if (!this.playing) {
      const threshold = this.hasStarted ? this.resumeSamples : this.preRollSamples;
      if (avail < threshold) {
        output.fill(0);
        return true;
      }
      // Crossed threshold — start (or resume) with a fade-in.
      this.playing    = true;
      this.hasStarted = true;
      this.fadeInLeft = this.fadeSamples;
      // Notify the main thread immediately (don't wait for the next stats
      // tick) so the renderer can start the idle→speech cross-fade promptly.
      this.port.postMessage({ type: 'playing', playing: true,
        playedSamples: this._playedSamples, sr: sampleRate });
    }

    // ── HEALTHY PATH: enough samples available for this tick ────────────
    if (avail >= N) {
      const src = this.buffer.subarray(this.readIndex, this.readIndex + N);

      if (this._silenceRamp) {
        // Silence-aware envelope + resume fade, sample-by-sample. Lookahead is
        // `fadeN` samples: if that window is entirely silent we ease the gain
        // toward 0 BEFORE the silence arrives (so the step into a [pause]/end is
        // ramped — no click), and ease back to 1 when voiced audio returns. The
        // inner scan `break`s on the first loud sample, so during speech it is
        // effectively O(1) and the gain stays at 1 → continuous joins untouched.
        const fadeN = this.fadeSamples;
        const buf   = this.buffer;
        const blen  = buf.length;
        const thr   = this._silThresh;
        for (let i = 0; i < N; i++) {
          let silentAhead = true;
          const p0 = this.readIndex + i;
          const p1 = Math.min(p0 + fadeN, blen);
          for (let p = p0; p < p1; p++) {
            if (Math.abs(buf[p]) > thr) { silentAhead = false; break; }
          }
          const target = silentAhead ? 0 : 1;
          if (this._silGain < target)      this._silGain = Math.min(target, this._silGain + 1 / fadeN);
          else if (this._silGain > target) this._silGain = Math.max(target, this._silGain - 1 / fadeN);
          let g = this._silGain;
          if (this.fadeInLeft > 0) {
            const remaining = Math.max(0, this.fadeInLeft - i);
            g *= 1 - (remaining / fadeN);
          }
          output[i] = src[i] * g;
        }
        if (this.fadeInLeft > 0) this.fadeInLeft = Math.max(0, this.fadeInLeft - N);
        const nowSilent = this._silGain < 0.05;
        if (nowSilent !== this._inSilence) {
          this._inSilence = nowSilent;
          console.log(`[GAP] ${nowSilent ? '→ eased OUT to silence' : '← eased IN from silence'} @ ${(this._playedSamples / sampleRate).toFixed(2)}s`);
        }
      } else if (this.fadeInLeft > 0) {
        // Resume ramp: gain 0→1 over fadeSamples
        const total = this.fadeSamples;
        for (let i = 0; i < N; i++) {
          const remaining = Math.max(0, this.fadeInLeft - i);
          const g         = 1 - (remaining / total);   // 0 at start of fade, 1 at end
          output[i] = src[i] * g;
        }
        this.fadeInLeft = Math.max(0, this.fadeInLeft - N);
      } else {
        output.set(src);
      }

      this.readIndex += N;
      this._playedSamples += N;
      this.lastSample = output[N - 1];
      return true;
    }

    // ── UNDERFLOW: not enough samples for this tick ─────────────────────
    // Fade out from lastSample → 0 over fadeSamples, then sustain silence.
    // Mark "playing=false" so the next refill triggers another pre-roll
    // wait + fresh fade-in.
    //
    // 2026-05-27 FIX: play any partial samples FIRST, then fade from the
    // last-actually-emitted sample to silence. The old code emitted a
    // fade from `lastSample` and then silenced the remaining N positions,
    // but discarded the partial samples sitting in the buffer
    // (`readIndex = buffer.length`). That created a content SKIP between
    // underrun and resume — audible as a click in browser playback even
    // though the recorded byte stream has none (recordings concatenate
    // the full byte stream with no real-time constraint). Playing the
    // partial first preserves the audio across the underrun event.
    let pos = 0;
    if (avail > 0) {
      const src = this.buffer.subarray(this.readIndex, this.readIndex + avail);
      if (this.fadeInLeft > 0) {
        const total = this.fadeSamples;
        for (let i = 0; i < avail; i++) {
          const remaining = Math.max(0, this.fadeInLeft - i);
          const g = 1 - (remaining / total);
          output[i] = src[i] * g;
        }
        this.fadeInLeft = Math.max(0, this.fadeInLeft - avail);
      } else {
        output.set(src);
      }
      this.lastSample = output[avail - 1];
      this.readIndex += avail;
      this._playedSamples += avail;
      pos = avail;
    }
    const fade = Math.min(this.fadeSamples, N - pos);
    for (let i = 0; i < fade; i++) {
      const g = 1 - (i / this.fadeSamples);
      output[pos + i] = this.lastSample * g;
    }
    for (let i = pos + fade; i < N; i++) {
      output[i] = 0;
    }
    if (this.playing) {
      // Notify the main thread IMMEDIATELY that playback stopped (don't wait
      // for the next ~256ms stats tick) so the renderer's end-of-turn idle
      // cross-fade fires right at audio-end instead of lingering.
      this.port.postMessage({ type: 'playing', playing: false,
        playedSamples: this._playedSamples, sr: sampleRate });
    }
    this.playing   = false;
    this.fadeInLeft = 0;
    this.lastSample = 0;
    this._underruns++;   // count the gap for the UI indicator
    return true;
  }

  // ── WSOLA time-stretch ───────────────────────────────────────────────
  // Append one stretched sample to the output FIFO, compacting/growing as
  // needed.
  _pushOut(v) {
    if (this._outLen >= this._outBuf.length) {
      if (this._outRead > 0) {
        this._outBuf.copyWithin(0, this._outRead, this._outLen);
        this._outLen -= this._outRead;
        this._outRead = 0;
      }
      if (this._outLen >= this._outBuf.length) {
        const g = new Float32Array(this._outBuf.length * 2);
        g.set(this._outBuf.subarray(0, this._outLen));
        this._outBuf = g;
      }
    }
    this._outBuf[this._outLen++] = v;
  }

  // Produce one WSOLA frame. The nominal analysis window is centered at
  // readIndex+SS; the ±SS similarity search picks the input window whose
  // leading overlap best matches the stored continuation target. CRITICAL:
  // the read cursor advances by exactly Ha per frame — the search offset
  // is a per-frame excursion that does NOT accumulate, otherwise for a
  // small factor the search snaps to the 1:1 continuation every frame and
  // no actual slowdown happens. Returns false when input is insufficient.
  _stretchFrame() {
    const W = this._W, Hs = this._Hs, Lov = this._Lov, Ha = this._Ha, SS = this._SS;
    if (this.readIndex + 2 * SS + W > this.buffer.length) return false;
    const center = this.readIndex + SS;

    // WSOLA similarity search (skip when no/low-energy target → nominal δ=0).
    let bestDelta = 0;
    if (this._target) {
      let tEnergy = 0;
      for (let i = 0; i < Lov; i++) tEnergy += this._target[i] * this._target[i];
      if (tEnergy > 1e-7) {
        let best = -Infinity;
        for (let d = -SS; d <= SS; d++) {
          const s = center + d;
          let dot = 0, cEnergy = 0;
          for (let i = 0; i < Lov; i++) {
            const c = this.buffer[s + i];
            dot += c * this._target[i];
            cEnergy += c * c;
          }
          const score = dot / Math.sqrt(cEnergy + 1e-9);
          if (score > best) { best = score; bestDelta = d; }
        }
      }
    }

    const p = center + bestDelta;            // chosen frame start (absolute)
    // Finalize Hs samples = previous overlap tail + windowed leading half.
    for (let i = 0; i < Hs; i++) {
      this._pushOut(this._olaTail[i] + this.buffer[p + i] * this._hann[i]);
    }
    // New overlap tail = windowed trailing half.
    for (let i = 0; i < Lov; i++) {
      this._olaTail[i] = this.buffer[p + Hs + i] * this._hann[Hs + i];
    }
    // Store the natural continuation (un-windowed) as next frame's target.
    if (!this._target) this._target = new Float32Array(Lov);
    for (let i = 0; i < Lov; i++) {
      this._target[i] = this.buffer[p + Hs + i];
    }
    // Nominal analysis advance — fixed Ha, independent of the search δ.
    this.readIndex += Ha;
    return true;
  }

  // Produce stretched output until the FIFO holds `minOut` samples or the
  // input runs short. Frame count is bounded as a runaway guard.
  _runStretch(minOut) {
    let guard = 512;
    while ((this._outLen - this._outRead) < minOut && guard-- > 0) {
      if (!this._stretchFrame()) break;
    }
  }

  // Stretched mirror of process(): same preroll / fade-in / underflow
  // machinery, but the sample source is the WSOLA output FIFO. _playedSamples
  // counts OUTPUT samples (the slowed timeline) so the video renderer can
  // scale frame advancement by the stretch factor and stay in sync.
  _processStretched(output, N) {
    const minOut = this.playing
      ? (N + this._Hs)
      : (this.hasStarted ? this.resumeSamples : this.preRollSamples) + N;
    this._runStretch(minOut);
    const availOut = this._outLen - this._outRead;

    if ((++this._statsTick % 48) === 0) {
      // Banked play-time = stretched FIFO + raw input re-scaled to output.
      const rawIn = this.buffer.length - this.readIndex;
      const banked = availOut + rawIn / this._stretchFactor;
      this.port.postMessage({
        type: 'buffer-stats',
        bufferedMs: Math.round((banked / sampleRate) * 1000),
        underruns:  this._underruns,
        playing:    this.playing,
        playedSamples: this._playedSamples,
        sr:         sampleRate,
      });
    }

    if (!this.playing) {
      const threshold = this.hasStarted ? this.resumeSamples : this.preRollSamples;
      if (availOut < threshold) { output.fill(0); return true; }
      this.playing    = true;
      this.hasStarted = true;
      this.fadeInLeft = this.fadeSamples;
      this.port.postMessage({ type: 'playing', playing: true,
        playedSamples: this._playedSamples, sr: sampleRate });
    }

    if (availOut >= N) {
      const r = this._outRead;
      if (this.fadeInLeft > 0) {
        const total = this.fadeSamples;
        for (let i = 0; i < N; i++) {
          const rem = Math.max(0, this.fadeInLeft - i);
          output[i] = this._outBuf[r + i] * (1 - rem / total);
        }
        this.fadeInLeft = Math.max(0, this.fadeInLeft - N);
      } else {
        for (let i = 0; i < N; i++) output[i] = this._outBuf[r + i];
      }
      this._outRead += N;
      this._playedSamples += N;
      this.lastSample = output[N - 1];
      if (this._outRead > 8192) {
        this._outBuf.copyWithin(0, this._outRead, this._outLen);
        this._outLen -= this._outRead;
        this._outRead = 0;
      }
      return true;
    }

    // Underflow: play whatever stretched samples remain, then fade to silence.
    let pos = 0;
    if (availOut > 0) {
      const r = this._outRead;
      if (this.fadeInLeft > 0) {
        const total = this.fadeSamples;
        for (let i = 0; i < availOut; i++) {
          const rem = Math.max(0, this.fadeInLeft - i);
          output[i] = this._outBuf[r + i] * (1 - rem / total);
        }
        this.fadeInLeft = Math.max(0, this.fadeInLeft - availOut);
      } else {
        for (let i = 0; i < availOut; i++) output[i] = this._outBuf[r + i];
      }
      this.lastSample = output[availOut - 1];
      this._outRead += availOut;
      this._playedSamples += availOut;
      pos = availOut;
    }
    const fade = Math.min(this.fadeSamples, N - pos);
    for (let i = 0; i < fade; i++) {
      output[pos + i] = this.lastSample * (1 - i / this.fadeSamples);
    }
    for (let i = pos + fade; i < N; i++) output[i] = 0;
    if (this.playing) {
      // Immediate playback-stopped signal (stretched path) — see normal path.
      this.port.postMessage({ type: 'playing', playing: false,
        playedSamples: this._playedSamples, sr: sampleRate });
    }
    this.playing    = false;
    this.fadeInLeft = 0;
    this.lastSample = 0;
    this._underruns++;
    return true;
  }
}

registerProcessor("pcm-player-processor", PCMPlayerProcessor);
