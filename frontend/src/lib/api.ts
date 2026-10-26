// Thin API client. Health data lives only in memory; only the auth token is kept (sessionStorage) so a
// refresh doesn't log staff out. Nothing is written to localStorage or caches (NFR-06).

const TOKEN_KEY = "skin.token";
let token: string | null = (() => {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
})();

export function setToken(t: string | null) {
  token = t;
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: keep in memory only */
  }
}

export function getToken() {
  return token;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

// `token` overrides the staff token: the client portal (/my) signs in separately and keeps its own token.
type Opts = { method?: string; body?: unknown; form?: FormData; idempotencyKey?: string; auth?: boolean; token?: string | null };

// Set by platformSso.ts: signs staff in again with the current WorkflowHub session after a 401 (session expired, or
// the platform user changed). Resolves true when a new staff token is in place.
let reauth: (() => Promise<boolean>) | null = null;
export function setReauth(fn: (() => Promise<boolean>) | null) {
  reauth = fn;
}

export async function api<T = any>(path: string, opts: Opts = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const bearer = opts.token !== undefined ? opts.token : token;
  if (opts.auth !== false && bearer) headers.Authorization = `Bearer ${bearer}`;
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const send = () => fetch(`/api${path}`, { method: opts.method ?? (body ? "POST" : "GET"), headers, body, cache: "no-store" });
  let res = await send();
  // Staff session refused: when opened from WorkflowHub, sign in again with the platform session and retry once.
  // (Not for the sign-in calls themselves; /auth/me is included, so an expired session found at start-up recovers.)
  if (res.status === 401 && opts.auth !== false && opts.token === undefined && bearer && reauth
      && !path.startsWith("/auth/login") && !path.startsWith("/auth/platform-session")) {
    if (await reauth()) {
      headers.Authorization = `Bearer ${token}`;
      res = await send();
    }
  }
  if (res.status === 401 && opts.auth !== false) {
    if (opts.token !== undefined) {
      window.dispatchEvent(new Event("client:expired"));
    } else {
      setToken(null);
      window.dispatchEvent(new Event("auth:expired"));
    }
  }
  if (!res.ok) {
    let msg = res.statusText;
    let code: string | undefined;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      code = j.code;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg, code);
  }
  return res.json();
}

// Images need the sign-in too, so they're fetched and shown from a blob. `token` and `auth` work as in api().
export async function fetchBlobUrl(path: string, opts: { token?: string | null; auth?: boolean } = {}): Promise<string> {
  const bearer = opts.token !== undefined ? opts.token : token;
  const headers: Record<string, string> = opts.auth !== false && bearer ? { Authorization: `Bearer ${bearer}` } : {};
  const res = await fetch(`/api${path}`, { headers, cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, "Could not load image");
  return URL.createObjectURL(await res.blob());
}

export function uid() {
  return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
}
