// End-to-end check of the talking avatar on the clinic device: the consultation's replies are said by the clinic's AI
// Avatar service (through the API's relay, in Direct mode), not by the browser's own voice. Needs the API with
// AVATAR_WS_URL and a platform service account set, and the web app. Then: node e2e/avatar.mjs
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const shots = join(here, "screenshots");
mkdirSync(shots, { recursive: true });
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const CHROME = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";
let step = 0;
const log = (m) => console.log(`[avatar ${String(++step).padStart(2, "0")}] ${m}`);

const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });
const notes = [];
try {
  const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  // Count the browser's own speech: with the avatar on, it must stay silent.
  await ctx.addInitScript(() => {
    window.__browserSpeech = 0;
    const orig = window.speechSynthesis?.speak?.bind(window.speechSynthesis);
    if (orig) window.speechSynthesis.speak = (u) => { window.__browserSpeech += 1; return orig(u); };
  });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (/avatar|synctalk/i.test(m.text())) notes.push(`${m.type()}: ${m.text()}`); });
  const frames = { sent: [], got: 0 };
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/api/avatar/ws")) return;
    ws.on("framesent", (f) => frames.sent.push(String(f.payload)));
    ws.on("framereceived", () => { frames.got += 1; });
  });

  await page.goto(`${BASE}/login`);
  await page.fill("#email", "operator@clinic.local");
  await page.fill("#password", "Demo@12345");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  await page.locator(".consent input[type=checkbox]").nth(0).check();
  await page.locator(".consent input[type=checkbox]").nth(1).check();
  await page.click("button:has-text('Start consultation')");
  await page.waitForURL(/\/session\//);
  log("consultation started");

  const t0 = Date.now();
  await page.waitForSelector("canvas.talking-avatar:not([hidden])", { timeout: 45000 });
  log(`avatar on screen after ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  await page.waitForSelector(".mirror.speaking", { timeout: 45000 });
  log(`greeting being said after ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  await page.waitForTimeout(1500);
  const drawn = await page.evaluate(() => {
    const c = document.querySelector("canvas.talking-avatar");
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    let opaque = 0;
    for (let i = 3; i < d.length; i += 4 * 97) if (d[i] > 0) opaque += 1;
    return { w: c.width, h: c.height, opaque };
  });
  if (!drawn.opaque) throw new Error("the avatar canvas is empty while speaking");
  await page.locator(".stagezone").screenshot({ path: join(shots, "avatar-1-greeting.png") });
  log(`canvas ${drawn.w}x${drawn.h} has the figure (${drawn.opaque} sampled pixels drawn); screenshot avatar-1-greeting.png`);
  await page.waitForSelector(".mirror:not(.speaking)", { timeout: 90000 });
  log(`greeting finished after ${((Date.now() - t0) / 1000).toFixed(1)}s`);

  await page.fill("#say", "98765 43210");
  await page.click("button:has-text('Send')");
  const t1 = Date.now();
  await page.waitForSelector(".mirror.speaking", { timeout: 60000 });
  log(`reply to the mobile number being said ${((Date.now() - t1) / 1000).toFixed(1)}s after sending`);
  await page.waitForTimeout(1200);
  await page.locator(".stagezone").screenshot({ path: join(shots, "avatar-2-reply.png") });
  await page.waitForSelector(".mirror:not(.speaking)", { timeout: 90000 });

  const speakLines = frames.sent.map((s) => JSON.parse(s)).filter((m) => m.type === "speak").map((m) => m.text);
  log(`lines sent to the avatar: ${speakLines.length}`);
  speakLines.forEach((l) => console.log("     • " + l.slice(0, 140)));
  const browserSpeech = await page.evaluate(() => window.__browserSpeech);
  if (browserSpeech) throw new Error(`the browser's own voice spoke ${browserSpeech} times`);
  if (speakLines.length < 2) throw new Error("expected the greeting and the reply to go to the avatar");
  log(`browser voice silent; ${frames.got} frames received from the relay`);
  console.log("PASS");
} catch (e) {
  console.log("FAIL:", e.message);
  process.exitCode = 1;
} finally {
  if (notes.length) console.log("console:\n  " + notes.slice(-15).join("\n  "));
  await browser.close();
}
