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
  const res = await fetch(`/api${path}`, { method: opts.method ?? (body ? "POST" : "GET"), headers, body, cache: "no-store" });
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

export async function fetchBlobUrl(path: string): Promise<string> {
  const res = await fetch(`/api${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, "Could not load image");
  return URL.createObjectURL(await res.blob());
}

export function uid() {
  return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
}
