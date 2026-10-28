/**
 * Wire format of the SyncTalk avatar backend (realtime_avatar_updated/backend,
 * `avatar.main3_optimized_sync`), decoded — never changed — here.
 *
 * Every binary message is `[1 byte opcode][payload]`, multi-byte fields
 * big-endian. Pure functions only: no DOM, no sockets, so they are unit-tested.
 */

/** A talking / transition frame: host video clock + the encoded image. */
export type SyncTalkFrame =
  | { kind: "video"; t: number; image: Uint8Array; chunkStart?: number }
  | { kind: "transition"; t: number; image: Uint8Array }
  | { kind: "audio"; pcm: Int16Array }
  | { kind: "body"; mode: number; bodyIdx: number }
  | { kind: "stop" }
  | { kind: "ignored"; opcode: number };

export const OP_VIDEO = 0x01;
export const OP_AUDIO = 0x02;
export const OP_BODY = 0x03;
export const OP_HOLD = 0x05;
export const OP_TRANSITION = 0x06;
export const OP_LIP_DELTA = 0x07;
export const OP_CLAUSE_FIRST = 0x08;
export const OP_MIC = 0x10;
export const OP_STOP = 0x12;

/**
 * Decode one binary message. A message too short for its opcode is reported
 * as `ignored` rather than thrown: one bad frame must not end the session.
 *
 *   0x01  [f32 video_t][image]                 talking frame
 *   0x08  [f32 video_t][f32 chunk_start][image] first frame of a TTS clause
 *   0x06  [f32 video_t][image]                 transition frame, no audio
 *   0x02  [pcm16 LE mono 24 kHz]               TTS audio, 40 ms per chunk
 *   0x03  [u8 mode][u32 body_idx]              0 idle / 1 lipsync / 2 hold
 *   0x05  keepalive or hold ping               nothing to draw
 *   0x12  hard stop
 */
export function parseSyncTalkFrame(data: Uint8Array): SyncTalkFrame {
  if (data.length === 0) return { kind: "ignored", opcode: -1 };
  const opcode = data[0];
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);

  if (opcode === OP_VIDEO || opcode === OP_TRANSITION) {
    if (data.length <= 5) return { kind: "ignored", opcode };
    const t = view.getFloat32(1, false);
    const image = data.slice(5);
    return opcode === OP_VIDEO
      ? { kind: "video", t, image }
      : { kind: "transition", t, image };
  }
  if (opcode === OP_CLAUSE_FIRST) {
    if (data.length <= 9) return { kind: "ignored", opcode };
    return {
      kind: "video",
      t: view.getFloat32(1, false),
      chunkStart: view.getFloat32(5, false),
      image: data.slice(9),
    };
  }
  if (opcode === OP_AUDIO) {
    // slice() copies, so the samples start on an even byte whatever the
    // message's own offset was; a trailing odd byte is dropped.
    const bytes = data.slice(1, 1 + ((data.length - 1) & ~1));
    if (bytes.length === 0) return { kind: "ignored", opcode };
    return {
      kind: "audio",
      pcm: new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length >> 1),
    };
  }
  if (opcode === OP_BODY) {
    if (data.length < 6) return { kind: "ignored", opcode };
    return { kind: "body", mode: view.getUint8(1), bodyIdx: view.getUint32(2, false) };
  }
  if (opcode === OP_STOP) return { kind: "stop" };
  // 0x05 (hold / keepalive) and 0x07 (lip delta, listening nod) carry nothing
  // this client draws: the backend ships complete figures for sync and noah.
  return { kind: "ignored", opcode };
}

// ── Idle atlas maths ────────────────────────────────────────────────────────

/** The atlas-meta fields the idle player reads (`*_meta.json`). */
export interface AtlasMeta {
  frame_count: number;
  grid_cols: number;
  grid_rows: number;
  tile_w: number;
  tile_h: number;
  pages?: number;
  fps?: number;
  pingpong?: boolean;
  cycle_count?: number;
  cells_per_page?: number;
  /** Bake stamp; changes whenever the pages are re-baked. */
  ver?: number | string;
}

/**
 * URL of atlas page `i`, versioned by the meta's bake stamp. The pages keep
 * their file names across re-bakes, and the service worker serves images
 * stale-while-revalidate, so an unversioned URL showed the previous bake on
 * the first visit after a deploy (Vedant's old cut against the new engine
 * matte: a 1.7 px outline jump at every idle <-> talk hand-off).
 */
export function atlasPageUrl(base: string, i: number, m: AtlasMeta): string {
  const url = `${base}_page${i}.webp`;
  return m.ver == null || m.ver === "" ? url : `${url}?v=${encodeURIComponent(String(m.ver))}`;
}

export interface AtlasCell {
  /** Frame index, clamped to [0, frame_count - 1]. */
  tile: number;
  page: number;
  col: number;
  row: number;
}

/** Length of one loop in frames: a pingpong atlas plays 0..N-1 then N-1..0. */
export function atlasCycleLen(m: AtlasMeta): number {
  const n = m.frame_count;
  return m.pingpong ? 2 * n : m.cycle_count || n;
}

/**
 * Page and grid cell of loop position `f` (already taken modulo the cycle
 * length; may be negative). The tile is clamped, so a meta file with a wrong
 * `cycle_count` can never index outside the atlas — that used to blank the
 * avatar for one frame per loop.
 */
export function atlasTileAndCell(m: AtlasMeta, f: number): AtlasCell {
  const n = m.frame_count;
  if (f < 0) f += atlasCycleLen(m);
  let tile = m.pingpong ? (f < n ? f : 2 * n - 1 - f) : f % n;
  if (tile < 0) tile = 0;
  else if (tile >= n) tile = n - 1;
  const per = m.cells_per_page ?? m.grid_cols * m.grid_rows;
  const inPage = tile % per;
  return {
    tile,
    page: (tile / per) | 0,
    col: inPage % m.grid_cols,
    row: (inPage / m.grid_cols) | 0,
  };
}

// ── Pixel helpers (RGBA byte arrays) ────────────────────────────────────────

/**
 * A "packed" frame is colour on top and the alpha matte (as grey) underneath,
 * so it is exactly twice as tall as it is wide for the square tiles in use.
 */
export function isPackedFrame(width: number, height: number): boolean {
  return width > 0 && height === 2 * width;
}

const LITTLE_ENDIAN = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;

/**
 * Split a packed frame into one straight-alpha image of half the height:
 * colour from the top half, alpha from the red channel of the pixel directly
 * below it in the bottom half.
 */
export function unpackPackedAlpha(
  packed: Uint8ClampedArray,
  width: number,
  packedHeight: number,
): Uint8ClampedArray {
  const height = packedHeight >> 1;
  const half = width * height * 4;
  const out = new Uint8ClampedArray(half);
  if (LITTLE_ENDIAN && packed.byteOffset % 4 === 0) {
    // One 32-bit move per pixel (RGB kept, A from the lower half): ~2x the
    // byte loop, and this runs on the main thread for every talking frame.
    const src = new Uint32Array(packed.buffer, packed.byteOffset, half >> 2);
    const dst = new Uint32Array(out.buffer);
    for (let p = 0, a = half; p < dst.length; p++, a += 4) {
      dst[p] = (src[p] & 0x00ffffff) | (packed[a] << 24);
    }
    return out;
  }
  for (let i = 0; i < half; i += 4) {
    out[i] = packed[i];
    out[i + 1] = packed[i + 1];
    out[i + 2] = packed[i + 2];
    out[i + 3] = packed[half + i];
  }
  return out;
}

/**
 * Green-key one page of the raw-green idle atlas, in place.
 *
 * key = g - max(r, b): above 70 is transparent, 8..70 a soft edge, below 8
 * opaque; green is then pulled down to the red/blue average to remove the
 * fringe. Alpha is only ever lowered — a pixel that was already transparent
 * must stay transparent, or the empty background turns into an opaque box.
 */
export function keyGreenIdlePage(data: Uint8ClampedArray): void {
  const hi = 70;
  const lo = 8;
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    const k = g - (r > b ? r : b);
    const keyed = k > hi ? 0 : k > lo ? ((hi - k) / (hi - lo)) * 255 : 255;
    if (keyed < data[i + 3]) data[i + 3] = keyed;
    const avg = (r + b) >> 1;
    if (g > avg) data[i + 1] = avg;
  }
}

/**
 * True when the visible pixels are near-black. The backend's last talking
 * frame of a turn can be all black; drawing it would flash the whole canvas.
 */
export function isNearBlack(data: Uint8ClampedArray): boolean {
  let sum = 0;
  let n = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] > 24) {
      sum += data[i] + data[i + 1] + data[i + 2];
      n++;
    }
  }
  return n > 32 && sum / (n * 3) < 10;
}
