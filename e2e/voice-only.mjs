// Voice-only mode on the clinic device: when the talking avatar can't be used, a Siri-style voice orb takes its place
// and the replies are still spoken, in the avatar's own voice from its speech engine (layer 2), or by the browser's
// voice if that engine can't be used either (layer 3). The avatar is made unavailable here by refusing its session.
// Needs the API (with AVATAR_WS_URL set) and the web app. Then: node e2e/voice-only.mjs
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
const log = (m) => console.log(`[voice ${String(++step).padStart(2, "0")}] ${m}`);

const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });

async function consultation({ engineDown }) {
  const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  await ctx.addInitScript(() => {
    window.__browserSpeech = 0;
    const orig = window.speechSynthesis?.speak?.bind(window.speechSynthesis);
    if (orig) window.speechSynthesis.speak = (u) => { window.__browserSpeech += 1; return orig(u); };
  });
  const page = await ctx.newPage();
  // The avatar can't be used (as when the platform refuses its sign-in).
  await page.route("**/api/avatar/session", (r) => r.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"test: avatar unavailable"}' }));
  if (engineDown) await page.route("**/api/avatar/speech", (r) => r.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"test: engine down"}' }));
  const speech = [];
  page.on("response", (res) => { if (res.url().endsWith("/api/avatar/speech")) speech.push(`${res.status()} ${res.headers()["content-type"] ?? ""}`); });
  await page.goto(`${BASE}/login`);
  await page.fill("#email", "operator@clinic.local");
  await page.fill("#password", "Demo@12345");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  await page.locator(".consent input[type=checkbox]").nth(0).check();
  await page.locator(".consent input[type=checkbox]").nth(1).check();
  await page.click("button:has-text('Start consultation')");
  await page.waitForURL(/\/session\//);
  return { ctx, page, speech };
}

const orbPixels = (page) => page.evaluate(() => {
  const c = document.querySelector("canvas.voice-orb");
  const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  let sum = 0;
  for (let i = 0; i < d.length; i += 4 * 211) sum += d[i] + d[i + 1] * 3 + d[i + 2] * 7 + d[i + 3];
  return { w: c.width, h: c.height, sum };
});

try {
  // Layer 2: the avatar's own voice behind the orb
  {
    const { ctx, page, speech } = await consultation({ engineDown: false });
    await page.waitForSelector("canvas.voice-orb", { timeout: 20000 });
    await page.waitForSelector(".voice-chip:has-text('Voice only')", { timeout: 20000 });
    log("avatar unavailable: the voice orb and 'Voice only' are shown in its place");
    if (await page.locator("canvas.talking-avatar:not([hidden])").count()) throw new Error("avatar canvas still shown");
    await page.waitForSelector(".mirror.speaking", { timeout: 30000 });
    await page.waitForTimeout(700);
    const a = await orbPixels(page);
    await page.waitForTimeout(250);
    const b = await orbPixels(page);
    if (!a.sum) throw new Error("the orb is empty");
    if (a.sum === b.sum) throw new Error("the orb is not moving while speaking");
    await page.locator(".stagezone").screenshot({ path: join(shots, "voice-1-speaking.png") });
    log(`greeting spoken; orb ${a.w}x${a.h} moving (frames differ); screenshot voice-1-speaking.png`);
    await page.waitForSelector(".mirror:not(.speaking)", { timeout: 60000 });
    await page.locator(".stagezone").screenshot({ path: join(shots, "voice-2-idle.png") });
    await page.fill("#say", "98765 43210");
    await page.click("button:has-text('Send')");
    await page.waitForSelector(".mirror.speaking", { timeout: 30000 });
    await page.waitForSelector(".mirror:not(.speaking)", { timeout: 60000 });
    const bs = await page.evaluate(() => window.__browserSpeech);
    log(`speech engine answers: ${speech.join(", ")}; browser voice used ${bs} times`);
    if (!speech.length || !speech.every((s) => s.startsWith("200 audio/wav"))) throw new Error("the avatar's voice was not used");
    if (bs) throw new Error("the browser's voice spoke although the avatar's voice worked");
    await ctx.close();
  }
  // Layer 3: the speech engine is down too: the browser's voice, same orb
  {
    const { ctx, page, speech } = await consultation({ engineDown: true });
    await page.waitForSelector("canvas.voice-orb", { timeout: 20000 });
    await page.waitForFunction(() => window.__browserSpeech > 0, null, { timeout: 30000 });
    log(`speech engine down (${speech.join(", ")}): the browser's voice speaks instead, behind the same orb`);
    await ctx.close();
  }
  console.log("PASS");
} catch (e) {
  console.log("FAIL:", e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
