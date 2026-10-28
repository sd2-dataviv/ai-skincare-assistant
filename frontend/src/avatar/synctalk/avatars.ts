/**
 * Which catalogue avatars the SyncTalk renderer can draw, and how. Ported from the AI Ecosystem web app
 * (components/AiAgent/synctalk/avatars.ts); the engine-socket settings are left out because this app only ever
 * receives the engine's frames relayed by the avatar service (the "relay" transport).
 *
 * Idle atlases live in public/avatar/avatars/<slug>/. Meera's optional movement library (~15 MB) isn't shipped: her
 * idle keeps its breathing and blinking without it.
 */

export type SyncTalkVariant = "sync" | "noah" | "vedant";

export interface SyncTalkAvatar {
  /** Avatar service catalogue id (`/api/avatars`). */
  avatarId: number;
  variant: SyncTalkVariant;
  /** `avatar_id` query value, from the engine's whitelist. */
  backendAvatarId: string;
  /** TTS voice gender sent with `set-voice`. */
  gender: "male" | "female";
  idle: {
    /** Folder under public/avatar/avatars/. */
    slug: string;
    /** Atlas base name: `<atlas>_meta.json`, `<atlas>_page<N>.webp`. */
    atlas: string;
    /** The atlas pages are raw green screen and are keyed once at load. */
    greenScreen: boolean;
    /** A movement library (`move_guides_manifest.json`) ships with it. */
    moveGuides: boolean;
  };
  /** Video buffered before a turn's playout starts, in seconds. */
  prefillS: number;
}

const AVATARS: ReadonlyArray<SyncTalkAvatar> = [
  {
    avatarId: 10,
    variant: "noah",
    backendAvatarId: "noah",
    gender: "male",
    idle: { slug: "noah-lblue", atlas: "idle_atlas", greenScreen: false, moveGuides: false },
    prefillS: 0.3,
  },
  {
    avatarId: 14,
    variant: "sync",
    backendAvatarId: "meera-sync",
    gender: "female",
    idle: { slug: "meera-sync", atlas: "idle_atlas_green", greenScreen: true, moveGuides: false },
    prefillS: 0.12,
  },
  {
    avatarId: 18,
    variant: "vedant",
    backendAvatarId: "vedant_sir",
    gender: "male",
    idle: { slug: "vedant-sir", atlas: "idle_atlas_alpha", greenScreen: false, moveGuides: false },
    prefillS: 0.3,
  },
];

/**
 * Leading byte of an engine frame the avatar service relays on the session socket
 * (`session_state.renderer === "synctalk"`); the rest is the engine's frame unchanged. Distinct from every avatar
 * service opcode (0x01–0x03, 0x12).
 */
export const SYNCTALK_RELAY_PREFIX = 0x20;

export function syncTalkAvatarFor(avatarId: number | null | undefined): SyncTalkAvatar | undefined {
  return avatarId == null ? undefined : AVATARS.find((a) => a.avatarId === avatarId);
}

/** Engine socket URL for the "socket" transport (unused here; kept so the ported player compiles unchanged). */
export function buildSyncTalkUrl(base: string, avatar: SyncTalkAvatar): string {
  const [beforeHash] = base.split("#", 1);
  const q = beforeHash.indexOf("?");
  const path = q === -1 ? beforeHash : beforeHash.slice(0, q);
  const params = new URLSearchParams(q === -1 ? "" : beforeHash.slice(q + 1));
  params.set("variant", avatar.variant);
  params.set("avatar_id", avatar.backendAvatarId);
  return `${path}?${params.toString()}`;
}
