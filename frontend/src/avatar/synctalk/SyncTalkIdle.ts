/**
 * The avatar's idle loop, drawn on the client.
 *
 * The SyncTalk backend only streams frames while the avatar talks (the client
 * declares `localIdle`), so between turns the figure comes from a sprite atlas
 * shipped with the app under public/assets/avatars/<slug>/: a pingpong
 * breathing/blinking loop, plus — where the avatar has one — a small library
 * of movement fragments spliced in at random intervals.
 *
 * Extracted from the reference front-end (realtime_avatar_updated/frontend,
 * _variant/transparent.ts: loadSyncIdleAtlas, loadMoveGuides,
 * _drawSyncIdleAtlas, forceIdleRestPhase, _keyLegacyIdlePage).
 */

import type { SyncTalkAvatar } from "./avatars";
import {
  atlasCycleLen,
  atlasPageUrl,
  atlasTileAndCell,
  keyGreenIdlePage,
  type AtlasMeta,
} from "./frames";

type Atlas = { meta: AtlasMeta; pages: ImageBitmap[] };

/** Idle must sit at rest this long before a movement fragment may start. */
const SETTLE_MS = 1500;
/** Ticks (40 ms) the idle takes to glide back to rest when a reply is on its
 *  way (the reply is sent ~0.4-0.6 s before its first audio comes back). */
const SETTLE_FRAMES = 12;

/** Glide from tile `from` back to the rest tile 0 in `frames` ticks, starting
 *  at time `at`. */
type Settling = { guide: Atlas | null; from: number; at: number; frames: number };

const closeAll = (pages: ImageBitmap[]) => {
  for (const p of pages) {
    try {
      p.close();
    } catch {
      /* already closed */
    }
  }
};

/** Straight (non-premultiplied) alpha: a premultiplied decode turns the
 *  transparent background of an atlas page into opaque black on some browsers. */
const decodeStraight = (blob: Blob) =>
  createImageBitmap(blob, { premultiplyAlpha: "none" });

/** Give the browser a turn between two long main-thread steps. */
const yieldToBrowser = () => new Promise<void>((r) => setTimeout(r, 0));

async function keyGreenPage(page: ImageBitmap): Promise<ImageBitmap> {
  const c = document.createElement("canvas");
  c.width = page.width;
  c.height = page.height;
  const cx = c.getContext("2d", { willReadFrequently: true });
  if (!cx) return page;
  cx.drawImage(page, 0, 0);
  const img = cx.getImageData(0, 0, c.width, c.height);
  keyGreenIdlePage(img.data);
  cx.putImageData(img, 0, 0);
  const keyed = await createImageBitmap(c);
  page.close();
  return keyed;
}

export class SyncTalkIdle {
  private atlas: Atlas | null = null;
  private guides: Atlas[] = [];
  private disposed = false;

  /** Origin of the idle loop's clock; frame 0 is the rest pose. */
  private t0 = 0;
  private restPhaseAt = 0;

  private guideActive = false;
  private guideIdx = 0;
  private guideStartT = 0;
  private guideNextAt = 0;
  private guidePrevF = 0;

  /** Set while gliding back to the rest frame at the start of a turn. */
  private settlingState: Settling | null = null;

  constructor(
    /** e.g. "/frontend/assets/avatars" */
    private readonly assetBase: string,
    private readonly avatar: SyncTalkAvatar,
  ) {}

  get ready(): boolean {
    return this.atlas !== null;
  }

  /** Load the idle loop; the movement library follows in the background. */
  async load(): Promise<void> {
    const { slug, atlas: name, greenScreen, moveGuides } = this.avatar.idle;
    const loaded = await this.loadAtlas(`${this.assetBase}/${slug}/${name}`, greenScreen);
    if (this.disposed) {
      closeAll(loaded.pages);
      return;
    }
    this.atlas = loaded;
    if (!this.t0) this.t0 = performance.now();
    if (moveGuides) {
      // Optional: without it the idle keeps its baked breathing and blinking.
      this.loadMoveGuides(`${this.assetBase}/${slug}`).catch((e) =>
        console.warn("[synctalk] movement library not loaded", e),
      );
    }
  }

  private async loadAtlas(base: string, greenScreen: boolean): Promise<Atlas> {
    // The meta is revalidated every time: its `ver` is what versions the pages.
    const meta: AtlasMeta = await this.fetchOk(`${base}_meta.json`, { cache: "no-cache" })
      .then((r) => r.json());
    const pages: ImageBitmap[] = [];
    try {
      for (let i = 0; i < (meta.pages ?? 1); i++) {
        let page = await this.fetchOk(atlasPageUrl(base, i, meta))
          .then((r) => r.blob())
          .then(decodeStraight);
        if (greenScreen) {
          // Keying a page reads ~4M pixels on the main thread.
          await yieldToBrowser();
          page = await keyGreenPage(page);
        }
        pages.push(page);
        if (this.disposed) break;
      }
    } catch (e) {
      closeAll(pages);
      throw e;
    }
    return { meta, pages };
  }

  private async loadMoveGuides(root: string): Promise<void> {
    const manifest: { guides?: string[] } = await this.fetchOk(
      `${root}/move_guides_manifest.json`,
    ).then((r) => r.json());
    const guides: Atlas[] = [];
    for (const name of manifest.guides ?? []) {
      if (this.disposed) break;
      guides.push(await this.loadAtlas(`${root}/${name}`, false));
    }
    if (this.disposed) {
      guides.forEach((g) => closeAll(g.pages));
      return;
    }
    this.guides = guides;
    this.guideNextAt = performance.now() + 2000 + Math.random() * 3000;
  }

  private async fetchOk(url: string, init?: RequestInit): Promise<Response> {
    const r = await fetch(url, init);
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r;
  }

  /**
   * Put the loop back on its rest frame now. Called at both hand-offs (idle to
   * talk, talk to idle) so the shoulders meet the talking frames' fixed pose
   * instead of wherever the breath happened to be.
   */
  forceRestPhase(): void {
    this.t0 = performance.now();
    this.restPhaseAt = this.t0;
    this.settlingState = null;
  }

  get settling(): boolean {
    return this.settlingState !== null;
  }

  /**
   * Glide back to the rest frame instead of freezing mid-breath, reaching it
   * within `frames` ticks: the engine's first frame IS the rest frame (RIFE
   * entry bridge), so the figure is already there when talking frames take
   * over. A glide already under way is only ever shortened, from wherever it
   * has got to.
   */
  settleToRest(frames: number = SETTLE_FRAMES): void {
    if (!this.atlas) return;
    const now = performance.now();
    const cur = this.settlingState;
    if (cur) {
      const fps = (cur.guide ?? this.atlas).meta.fps || 25;
      const left = cur.frames - ((now - cur.at) / 1000) * fps;
      if (left > frames) {
        this.settlingState = { guide: cur.guide, from: this.settlingTile(cur, now), at: now, frames };
      }
      return;
    }
    let s: Settling;
    if (this.guideActive) {
      const g = this.guides[this.guideIdx];
      const gf = Math.floor(((now - this.guideStartT) / 1000) * (g.meta.fps || 25));
      // A fragment starts and ends at rest: run it back to its start.
      s = { guide: g, from: Math.min(gf, g.meta.frame_count - 1), at: now, frames };
    } else {
      const m = this.atlas.meta;
      const f = Math.floor(((now - this.t0) / 1000) * (m.fps || 25)) % atlasCycleLen(m);
      s = { guide: null, from: atlasTileAndCell(m, f).tile, at: now, frames };
    }
    this.settlingState = s;
    this.guideActive = false;
  }

  /** Tile a glide shows at time `now`. */
  private settlingTile(s: Settling, now: number): number {
    // Reach rest within s.frames ticks whatever the distance, on an
    // ease-in-out curve: a constant-speed rewind (up to 13 tiles a tick out of
    // a movement fragment) snapped the head back — the jerk at a reply's start.
    const fps = (s.guide ?? this.atlas)?.meta.fps || 25;
    const p = Math.min(1, ((now - s.at) / 1000) * fps / s.frames);
    return Math.round(s.from * (1 - p * p * (3 - 2 * p)));
  }

  private drawSettling(ctx: CanvasRenderingContext2D, w: number, h: number, s: Settling): boolean {
    const src = s.guide ?? this.atlas;
    if (!src) return false;
    const cell = atlasTileAndCell(src.meta, this.settlingTile(s, performance.now()));
    const page = src.pages[cell.page];
    if (!page) return false;
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(page, cell.col * src.meta.tile_w, cell.row * src.meta.tile_h,
      src.meta.tile_w, src.meta.tile_h, 0, 0, w, h);
    return true;
  }

  /** Draw the current idle frame over the whole canvas. False if not loaded. */
  draw(ctx: CanvasRenderingContext2D, w: number, h: number): boolean {
    const atlas = this.atlas;
    if (!atlas || atlas.pages.length === 0) return false;
    if (this.settlingState) return this.drawSettling(ctx, w, h, this.settlingState);
    const m = atlas.meta;
    const now = performance.now();
    let f = Math.floor(((now - this.t0) / 1000) * (m.fps || 25)) % atlasCycleLen(m);

    if (this.guides.length) {
      // A fragment starts and ends on the rest frame, so it may only begin as
      // the loop wraps — and not right after a hand-off reset, which also
      // reads as a wrap.
      const settled = now - this.restPhaseAt >= SETTLE_MS;
      if (
        !this.guideActive &&
        settled &&
        now >= this.guideNextAt &&
        (f < this.guidePrevF || f === 0)
      ) {
        this.guideActive = true;
        this.guideStartT = now;
        this.guideIdx = (Math.random() * this.guides.length) | 0;
      }
      this.guidePrevF = f;
      if (this.guideActive) {
        const g = this.guides[this.guideIdx];
        const gf = Math.floor(((now - this.guideStartT) / 1000) * (g.meta.fps || 25));
        if (gf >= g.meta.frame_count) {
          this.guideActive = false;
          this.guideNextAt = now + 2500 + Math.random() * 4500;
          // The fragment ended at rest: resume the loop from its rest frame,
          // this tick included, rather than from a mid-breath frame.
          this.t0 = now;
          f = 0;
        } else {
          const cell = atlasTileAndCell(g.meta, gf);
          const page = g.pages[cell.page];
          if (page) {
            ctx.clearRect(0, 0, w, h);
            ctx.drawImage(
              page,
              cell.col * g.meta.tile_w,
              cell.row * g.meta.tile_h,
              g.meta.tile_w,
              g.meta.tile_h,
              0,
              0,
              w,
              h,
            );
            return true;
          }
        }
      }
    }

    const cell = atlasTileAndCell(m, f);
    const page = atlas.pages[cell.page];
    if (!page) return false;
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(page, cell.col * m.tile_w, cell.row * m.tile_h, m.tile_w, m.tile_h, 0, 0, w, h);
    return true;
  }

  dispose(): void {
    this.disposed = true;
    if (this.atlas) closeAll(this.atlas.pages);
    this.guides.forEach((g) => closeAll(g.pages));
    this.atlas = null;
    this.guides = [];
  }
}
