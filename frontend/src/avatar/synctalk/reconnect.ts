/**
 * Reconnect policy for the SyncTalk face socket: bounded exponential backoff,
 * and no retry at all for a close the server meant.
 */

/** 1008 and the whole application range 4000-4999 are not transient (AI Ecosystem utils/wsAuth.ts). */
export function isFatalClose(code: number): boolean {
  return code === 1008 || (code >= 4000 && code <= 4999);
}

export const FACE_MAX_RETRIES = 10;
/**
 * Retries for a backend that has never answered on this mount. Kept short:
 * the page falls back to the avatar service's own face once the engine gives
 * up, and nobody should wait minutes for that.
 */
export const FACE_FIRST_CONNECT_RETRIES = 2;
const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;

/** 1013: the backend is at its session cap and said so before closing. */
export const CLOSE_TRY_AGAIN_LATER = 1013;

/**
 * Milliseconds to wait before reconnect number `attempt` (0-based), or null
 * when the socket must not be reopened without the user asking: a refused
 * credential or policy close (1008, 4000-4999), a server at capacity, or the
 * retry budget spent. `everReady` says whether the backend has announced the
 * avatar ready at least once on this mount.
 */
export function faceReconnectDelay(
  code: number,
  attempt: number,
  everReady = true,
): number | null {
  if (isFatalClose(code) || code === CLOSE_TRY_AGAIN_LATER) return null;
  if (attempt >= (everReady ? FACE_MAX_RETRIES : FACE_FIRST_CONNECT_RETRIES)) return null;
  return Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
}
