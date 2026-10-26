/* Platform sign-in: staff are signed in automatically when WorkflowHub already knows them
 * (WorkflowHub "Building an Application" guide, section 4; docs/08-platform-sso.md).
 *
 * Where the platform token comes from, best first:
 *   1. window.__wfhSso.token(): the helper WorkflowHub's gate adds to the page. It always has the CURRENT token: from
 *      #sso_token on the first visit and from the gate's cookie on every later visit, reload or new tab.
 *   2. window.__wfhSsoToken: published by the gate's /sso.js on older set-ups.
 *   3. #sso_token=... in the address (first visit). It is removed from the address bar at once.
 *
 * The platform token is sent only to our own backend (POST /api/auth/platform-session), which checks it with the
 * platform's auth service and returns a normal staff session (the same as the login form). The browser never says who
 * the user is. Only the last 24 characters of a platform token are ever kept, to recognise it again.
 *
 * `ssoReady` is awaited in main.tsx before the first render, so the app never flashes "signed out".
 *
 * Behaviour (as in the guide):
 *   - first visit, later visit, reload, new tab: signed in;
 *   - the platform user signs in again or as someone else: the app follows the new platform session;
 *   - our session expires (401): signed in again once with the current platform session and the call retried;
 *   - Sign out in this app: stays signed out for that platform login; a new platform login signs in again;
 *   - opened outside WorkflowHub: nothing happens, the login form works as before.
 */
import { getToken, setReauth, setToken } from "./api";

declare global {
  interface Window {
    __wfhSso?: { token?: () => Promise<string | null> | string | null };
    __wfhSsoToken?: string | null;
  }
}

const MARK = "skin.sso_mark"; // which platform login our staff session came from
const SIGNED_OUT = "skin.sso_signed_out"; // the platform login the user signed out of
const mark = (t: string | null) => (t ? t.slice(-24) : null);

// sessionStorage like the staff token itself (NFR-06: nothing in localStorage).
function ss(key: string, value?: string | null): string | null {
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch {
    /* storage blocked: the exchange still works for this page */
  }
  return null;
}

function fromFragment(): string | null {
  const hash = window.location.hash || "";
  const m = hash.match(/[#&]sso_token=([^&]+)/);
  if (!m) return null;
  const rest = hash.replace(/[#&]sso_token=[^&]*/, "").replace(/^&/, "#");
  try {
    window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search
      + (rest && rest !== "#" ? rest : ""));
  } catch {
    /* the token stays visible; the exchange still works */
  }
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return null;
  }
}

/** The token of whoever is signed in to the platform right now, or null (opened outside WorkflowHub). */
async function platformToken(): Promise<string | null> {
  const fragment = fromFragment(); // always strip it, whichever source wins
  const host = window.__wfhSso;
  if (host && typeof host.token === "function") {
    try {
      const t = await host.token();
      if (t) return t;
    } catch {
      /* fall back */
    }
  }
  return window.__wfhSsoToken || fragment;
}

async function exchange(platform: string): Promise<boolean> {
  try {
    const res = await fetch("/api/auth/platform-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: platform }),
      cache: "no-store",
    });
    if (!res.ok) {
      // 401 bad/expired token, 403 no clinic account or disabled, 409 no email: the login form is the answer.
      console.warn("[SSO] sign-in refused:", res.status);
      return false;
    }
    const d = (await res.json()) as { access_token: string };
    setToken(d.access_token);
    ss(MARK, mark(platform));
    ss(SIGNED_OUT, null);
    return true;
  } catch (e) {
    console.warn("[SSO] sign-in failed:", e instanceof Error ? e.message : e); // never block the app on it
    return false;
  }
}

async function signIn(force = false): Promise<boolean> {
  const platform = await platformToken();
  if (!platform) return false; // opened outside WorkflowHub
  const m = mark(platform);
  if (!force && m === ss(SIGNED_OUT)) return false; // signed out of this platform login
  if (!force && getToken()) {
    const from = ss(MARK);
    if (!from || from === m) return false; // same platform login, or a deliberate password sign-in: keep it
  }
  return exchange(platform); // new platform login or another user: follow the platform
}

/** Call before Sign out: this platform login won't sign in again on its own; a new one will. */
export async function markSignedOut(): Promise<void> {
  ss(SIGNED_OUT, mark(await platformToken()) ?? ss(SIGNED_OUT));
  ss(MARK, null);
}

let renewing: Promise<boolean> | null = null;
setReauth(async () => {
  // One sign-in for any number of calls that hit 401 together.
  renewing ??= signIn(true).then((ok) => {
    if (ok) window.dispatchEvent(new Event("auth:renewed"));
    return ok;
  }).finally(() => { renewing = null; });
  return renewing;
});

export const ssoReady: Promise<boolean> = signIn();

// The skin models need a valid platform token. When the platform won't give the app a session of its own (that is
// admin-only), the open app passes on the person's current token, which WorkflowHub keeps renewing. Inside
// WorkflowHub only; failures are ignored (the models then fall back to estimates and the doctor is told why).
const MODEL_TOKEN_EVERY_MS = 4 * 60 * 1000;
async function passTokenToModels(): Promise<void> {
  if (!getToken()) return;
  const platform = await platformToken();
  if (!platform) return;
  try {
    await fetch("/api/auth/platform-token", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
      body: JSON.stringify({ token: platform }),
      cache: "no-store",
    });
  } catch {
    /* offline for a moment: try again next time */
  }
}
ssoReady.then(() => {
  if (!window.__wfhSso && !window.__wfhSsoToken) return; // opened outside WorkflowHub
  void passTokenToModels();
  window.setInterval(() => { void passTokenToModels(); }, MODEL_TOKEN_EVERY_MS);
  window.addEventListener("auth:renewed", () => { void passTokenToModels(); });
});
