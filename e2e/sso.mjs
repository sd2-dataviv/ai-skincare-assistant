// End-to-end check of WorkflowHub platform sign-in in a real browser, with a stand-in auth service
// (e2e/fake_platform_auth.py) and a stand-in for the gate's window.__wfhSso helper. Checks the guide's behaviour:
// first visit, reload, the platform user changing, Sign out, an expired session, the #sso_token route, and a
// platform user with no clinic account.
// Prereqs: API with WFH_AUTH_SERVICE_URL pointing at the stand-in, and the web app. Then: node e2e/sso.mjs
import { chromium } from "playwright-core";

const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const CHROME = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";
const T = {
  doctor: "PT-doctor-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  doctor2: "PT-doctor-bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  admin: "PT-admin-cccccccccccccccccccccccccccccc",
  stranger: "PT-stranger-dddddddddddddddddddddddddd",
};
let step = 0;
const log = (m) => console.log(`[sso ${String(++step).padStart(2, "0")}] ${m}`);
const expect = (ok, what) => { if (!ok) throw new Error(what); };

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
try {
  // The gate's helper: always hands over whoever is signed in to the platform right now.
  let current = T.doctor;
  const ctx = await browser.newContext();
  await ctx.exposeFunction("__testPlatformToken", () => current);
  await ctx.addInitScript(() => { window.__wfhSso = { token: () => window.__testPlatformToken() }; });
  const page = await ctx.newPage();
  let exchanges = 0;
  page.on("request", (r) => { if (r.url().endsWith("/api/auth/platform-session")) exchanges++; });
  const who = () => page.evaluate(async () => {
    const t = sessionStorage.getItem("skin.token");
    if (!t) return null;
    const r = await fetch("/api/auth/me", { headers: { Authorization: `Bearer ${t}` } });
    return r.ok ? (await r.json()).email : `HTTP ${r.status}`;
  });
  const settle = async () => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(300); };

  await page.goto(`${BASE}/`);
  await settle();
  expect((await who()) === "doctor@clinic.local" && page.url().includes("/doctor"), `first visit: ${await who()} at ${page.url()}`);
  log(`first visit: signed in as doctor@clinic.local without the form (${exchanges} exchange)`);

  const before = exchanges;
  await page.reload();
  await settle();
  expect((await who()) === "doctor@clinic.local" && exchanges === before, "reload should keep the session without a new exchange");
  log("reload: still signed in, no new exchange");

  current = T.admin;
  await page.reload();
  await settle();
  expect((await who()) === "admin@clinic.local", `platform user changed: got ${await who()}`);
  log("platform user changed to the admin: the app followed");

  await page.click("button:has-text('Sign out')");
  await page.waitForURL((u) => u.pathname.startsWith("/login"));
  await page.reload();
  await settle();
  expect(page.url().includes("/login") && (await who()) === null, "after Sign out the same platform login must not sign in again");
  log("Sign out: stays signed out on reload with the same platform login");

  current = T.doctor2; // a new platform login
  await page.reload();
  await settle();
  expect((await who()) === "doctor@clinic.local", "a new platform login should sign in again");
  log("new platform login: signed in again automatically");

  await page.evaluate(() => sessionStorage.setItem("skin.token", "expired.or.revoked"));
  const seen = new Map();
  const count = (r) => { const p = new URL(r.url()).pathname; if (p.startsWith("/api/")) seen.set(p, (seen.get(p) ?? 0) + 1); };
  page.on("request", count);
  await page.goto(`${BASE}/doctor`);
  await page.waitForTimeout(4000);
  page.off("request", count);
  const busiest = [...seen.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);
  expect(busiest.every(([, n]) => n <= 6), `requests keep repeating after renewal: ${JSON.stringify(busiest)}`);
  expect((await who()) === "doctor@clinic.local" && page.url().includes("/doctor"), `expired session: ${await who()} at ${page.url()}`);
  log("expired staff session: signed in again with the platform session and carried on");

  // Fragment route (older gate): no helper, token in the address, removed from the address bar
  const ctx2 = await browser.newContext();
  const p2 = await ctx2.newPage();
  await p2.goto(`${BASE}/#sso_token=${T.doctor}`);
  await p2.waitForLoadState("networkidle");
  const url2 = p2.url();
  const t2 = await p2.evaluate(() => sessionStorage.getItem("skin.token"));
  expect(t2 && !url2.includes("sso_token"), `fragment route: token ${!!t2}, url ${url2}`);
  expect(!(await p2.evaluate(() => JSON.stringify(sessionStorage))).includes(T.doctor), "the whole platform token must not be stored");
  log("#sso_token route: signed in, token removed from the address bar, never stored whole");

  // A platform user with no clinic account sees the normal form
  const ctx3 = await browser.newContext();
  await ctx3.addInitScript((t) => { window.__wfhSso = { token: async () => t }; }, T.stranger);
  const p3 = await ctx3.newPage();
  await p3.goto(`${BASE}/`);
  await p3.waitForURL((u) => u.pathname.startsWith("/login"));
  log("platform user without a clinic account: the normal login form");

  // Outside WorkflowHub: nothing changes
  const p4 = await (await browser.newContext()).newPage();
  await p4.goto(`${BASE}/`);
  await p4.waitForURL((u) => u.pathname.startsWith("/login"));
  await p4.fill("#email", "operator@clinic.local");
  await p4.fill("#password", "Demo@12345");
  await p4.click("button:has-text('Sign in')");
  await p4.waitForURL((u) => !u.pathname.startsWith("/login"));
  log("outside WorkflowHub: the login form works as before");
  console.log("SSO E2E PASS");
} catch (e) {
  console.error("SSO E2E FAIL:", e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
