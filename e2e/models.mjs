// End-to-end check of the skin-model pictures in a real browser: the doctor sees the marked-up photos, the model's
// scores and the post-treatment preview while reviewing; the clinic device and the client's phone show nothing until
// the plan is approved, then show the pictures with the preview labelled as an illustration. Also checks phone width.
// Prereqs: the stand-in models (scripts/fake_skin_models.py --no-face-check), an API with LLM_PROVIDER=scripted,
// PHOTO_FACE_CHECK=false, ANALYSIS_ENGINE=ws pointing at them, and the web app. Then: node e2e/models.mjs
import { chromium } from "playwright-core";
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const shots = join(here, "screenshots");
mkdirSync(shots, { recursive: true });
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const API = process.env.API_URL ?? "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";
const PY = process.env.PYTHON ?? join(here, "..", "backend", ".venv", "bin", "python");
let step = 0;
const log = (m) => console.log(`[models ${String(++step).padStart(2, "0")}] ${m}`);

log("consultation through the chat, up to the doctor's approval…");
const made = JSON.parse(execFileSync(PY, [join(here, "model_visit.py")], { encoding: "utf8", env: { ...process.env, API_URL: API } })
  .trim().split("\n").pop());
log(`visit ${made.visit_id.slice(0, 8)} is at "${made.stage}"`);

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const noSideScroll = async (page, what) => {
  const o = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (o > 1) throw new Error(`${what} scrolls sideways on a phone by ${o}px`);
};
const loaded = (page, sel) => page.waitForFunction((s) => {
  const imgs = [...document.querySelectorAll(s)];
  return imgs.length > 0 && imgs.every((i) => i.complete && i.naturalWidth > 0);
}, sel, { timeout: 30000 });
async function login(ctx, email) {
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login`);
  await page.fill("#email", email);
  await page.fill("#password", "Demo@12345");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  return page;
}

try {
  // 1. Doctor's review
  const dr = await login(await browser.newContext({ viewport: { width: 1440, height: 1100 } }), "doctor@clinic.local");
  await dr.goto(`${BASE}/doctor/visit/${made.visit_id}`);
  await dr.waitForSelector(".model-pics .mp-grid figure");
  await loaded(dr, ".model-pics .mp-grid img");
  const tags = await dr.locator(".finding .src").allInnerTexts();
  if (!tags.some((t) => t.startsWith("model"))) throw new Error("no score is marked as coming from the model");
  if (!tags.includes("estimate")) throw new Error("estimates are not marked");
  log(`review: ${await dr.locator(".model-pics .mp-grid figure").count()} marked-up photos; scores tagged ${[...new Set(tags.map((t) => t.split(" ")[0]))].join(" / ")}`);
  log("overlay captions: " + (await dr.locator(".model-pics .mp-grid figcaption").allInnerTexts()).map((t) => t.replace(/\n/g, ": ")).join(" | "));
  await dr.waitForSelector(".mp-preview .ba img", { timeout: 60000 });  // the review polls while the preview is made
  await loaded(dr, ".mp-preview .ba img");
  const pill = await dr.locator(".mp-preview .pill").innerText();
  if (pill !== "Client sees this after approval") throw new Error(`preview pill says "${pill}"`);
  log(`preview ready (before/after); "${pill}"`);
  await dr.locator(".model-pics").scrollIntoViewIfNeeded();
  await dr.screenshot({ path: join(shots, "m1-doctor-review.png"), fullPage: true });

  // 2. Clinic device before approval: no pictures
  const op = await login(await browser.newContext({ viewport: { width: 1280, height: 900 } }), "operator@clinic.local");
  await op.goto(`${BASE}/session/${made.conversation_id}`);
  await op.click("button[role=tab]:has-text('Results')");
  await op.waitForSelector(".results .findings");
  if (await op.locator(".model-pics").count()) throw new Error("the clinic device shows model pictures before approval");
  log("clinic device before approval: findings shown, no model pictures");

  // 3. Doctor approves
  await dr.click("button:has-text('Approve plan')");
  await dr.waitForURL((u) => u.pathname === "/doctor");
  log("doctor approved the plan");

  // 4. Clinic device after approval
  await op.reload();
  await op.click("button[role=tab]:has-text('Results')");
  await op.waitForSelector(".results .model-pics .mp-preview");
  await loaded(op, ".results .model-pics img");
  const label = await op.locator(".results .illustration-tag").innerText();
  const caption = await op.locator(".results .mp-preview p.tiny").innerText();
  if (label !== "Illustration only" || !caption.includes("not a promise")) throw new Error("preview is not labelled as an illustration");
  log(`clinic device after approval: ${await op.locator(".results .mp-grid figure").count()} photos + preview labelled "${label}"`);
  await op.locator(".results .model-pics").scrollIntoViewIfNeeded();
  await op.screenshot({ path: join(shots, "m2-device-after-approval.png"), fullPage: true });

  // 5. The client's phone
  const ph = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true })).newPage();
  await ph.goto(`${BASE}/my`);
  await ph.fill("#m", "9876543210");
  await ph.click("button:has-text('Send me a code')");
  await ph.fill("#c", "123456");
  await ph.click("button:has-text('Sign in')");
  await ph.waitForSelector(".past-visit");
  await ph.goto(`${BASE}/my/visits/${made.visit_id}`);
  await ph.waitForSelector(".letter .model-pics .mp-preview");
  await loaded(ph, ".letter .model-pics img");
  await noSideScroll(ph, "/my/visits/:id");
  log(`phone summary: ${await ph.locator(".letter .model-pics img").count()} pictures, "${await ph.locator(".letter .illustration-tag").innerText()}", fits the width`);
  await ph.locator(".letter .model-pics").scrollIntoViewIfNeeded();
  await ph.screenshot({ path: join(shots, "m3-phone-summary.png"), fullPage: true });
  console.log("MODELS E2E PASS");
} catch (e) {
  console.error("MODELS E2E FAIL:", e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
