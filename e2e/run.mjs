// End-to-end check of the full consultation in a real browser (system Chrome, separate temp profile).
// Prereqs: API on :8000 started with PHOTO_FACE_CHECK=false LLM_PROVIDER=scripted, web on :5173.
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const shots = join(here, "screenshots");
mkdirSync(shots, { recursive: true });
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const CHROME = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";
const PW = "Demo@12345";
let step = 0;
const log = (m) => console.log(`[e2e ${String(++step).padStart(2, "0")}] ${m}`);

const browser = await chromium.launch({
  executablePath: CHROME,
  headless: true,
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
         `--use-file-for-fake-video-capture=${join(here, "fake-cam.y4m")}`],
});

async function login(ctx, email) {
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login`);
  await page.fill("#email", email);
  await page.fill("#password", PW);
  await page.click("button:has-text('Sign in')");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  return page;
}

async function say(page, text) {
  await page.fill("#say", text);
  await page.click("form.send-row button");
  await page.waitForFunction(() => !document.querySelector("#say")?.disabled, null, { timeout: 30000 });
}

const lastAssistant = (page) => page.locator(".msg.assistant .bubble").last().innerText();

try {
  const opCtx = await browser.newContext({ viewport: { width: 1366, height: 900 }, permissions: ["camera"] });
  const op = await login(opCtx, "operator@clinic.local");
  log("operator signed in");
  await op.screenshot({ path: join(shots, "01-consent.png") });
  await op.check("text=I agree to talk with an AI assistant");
  await op.check("text=I agree that the conversation");
  await op.click("button:has-text('Start consultation')");
  await op.waitForSelector(".msg.assistant");
  log("session started: " + (await lastAssistant(op)).slice(0, 60));

  await say(op, "9876543210");
  await op.waitForSelector("input[placeholder='6-digit code']");
  await say(op, "123456");
  await op.waitForSelector("text=Welcome", { timeout: 15000 }).catch(() => undefined);
  log("identified: " + (await lastAssistant(op)).slice(0, 60));
  // A returning client sees their own past consultations
  await op.click("button[role=tab]:has-text('History')");
  await op.waitForSelector(".past-visit");
  await op.waitForTimeout(500);
  await op.screenshot({ path: join(shots, "03b-history.png") });
  await op.click("button[role=tab]:has-text('Conversation')");
  log("history tab shows past consultations");
  await say(op, "I have dark spots on my cheeks and some acne on my forehead");
  await op.waitForSelector(".question-head");
  await op.screenshot({ path: join(shots, "02-know-your-skin.png") });
  log("questionnaire started");

  for (let i = 0; i < 25; i++) {
    if (await op.locator("button.shutter").count()) break;
    const chip = op.locator(".chip").first();
    await chip.click();
    await op.waitForFunction(() => !document.querySelector("#say")?.disabled, null, { timeout: 30000 });
  }
  await op.waitForSelector("button.shutter", { timeout: 15000 });
  await op.waitForTimeout(1500); // let the camera stream start
  await op.screenshot({ path: join(shots, "03-camera.png") });
  log("camera opened");

  for (const pose of ["front", "left", "right"]) {
    await op.click("button.shutter");
    await op.waitForFunction(() => !document.querySelector("#say")?.disabled, null, { timeout: 60000 });
    await op.waitForTimeout(800);
    log(`photo ${pose}: ` + (await lastAssistant(op)).slice(0, 70));
  }
  await op.waitForSelector("text=Here's what I found", { timeout: 60000 });
  await op.waitForSelector(".banner.wait", { timeout: 30000 });
  await op.screenshot({ path: join(shots, "04-analysis-and-draft.png") });
  log("analysis + draft shown, waiting for doctor");

  await op.click("button[role=tab]:has-text('Results')");
  await op.waitForSelector(".finding");
  await op.waitForTimeout(800);
  await op.screenshot({ path: join(shots, "05-results.png"), fullPage: true });
  log("results tab rendered");

  const drCtx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const dr = await login(drCtx, "doctor@clinic.local");
  await dr.waitForSelector(".queue a:has-text('Draft plan awaiting approval')");
  await dr.locator(".queue a:has-text('Draft plan awaiting approval')").last().click();
  await dr.waitForSelector("text=Approve plan");
  await dr.waitForTimeout(800);
  await dr.screenshot({ path: join(shots, "06-doctor-review.png"), fullPage: true });
  log("doctor opened the draft");
  await dr.click("button:has-text('Approve plan')");
  await dr.waitForURL((u) => u.pathname === "/doctor");
  log("doctor approved");

  await op.click("button[role=tab]:has-text('Conversation')");
  await op.waitForSelector("text=approved your plan", { timeout: 20000 });
  // Book the plan's treatments into a procedure room: tap the first offered time for each one.
  await op.waitForSelector(".chips.slots .chip:not(.quiet)", { timeout: 20000 });
  await op.waitForTimeout(500);
  await op.screenshot({ path: join(shots, "07-booking-offer.png") });
  log("client offered room slots: " + (await lastAssistant(op)).slice(0, 80));
  for (let i = 0; i < 4 && (await op.locator(".chips.slots .chip:not(.quiet)").count()); i++) {
    const before = await op.locator(".msg.assistant").count();
    await op.locator(".chips.slots .chip:not(.quiet)").first().click();
    await op.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, before, { timeout: 20000 });
    await op.waitForTimeout(400);
  }
  await op.waitForSelector("text=You're all set", { timeout: 20000 });
  await op.waitForSelector(".banner.ok");
  await op.screenshot({ path: join(shots, "08-booked.png") });
  log("treatments booked: " + (await lastAssistant(op)).slice(0, 80));

  await op.click(".banner.ok a");
  await op.waitForSelector(".letter .appts");
  if (await op.locator(".topnav, a[href='/ot']").count()) throw new Error("staff navigation visible to the client");
  await op.screenshot({ path: join(shots, "09-summary.png"), fullPage: true });
  log("summary shows the appointments");

  // Back from the summary returns to the same, finished conversation; it must not look like a new session.
  await op.goBack();
  await op.waitForSelector("text=You're all set", { timeout: 15000 });
  if ((await op.locator(".msg.assistant").count()) < 5) throw new Error("going back showed a fresh session");
  await op.goForward();
  await op.waitForSelector(".letter .appts");
  await op.click("text=Back to the conversation");
  await op.waitForSelector("text=You're all set", { timeout: 15000 });
  if ((await op.locator(".msg.assistant").count()) < 5) throw new Error("the back link showed a fresh session");
  log("back from the summary returns to the same conversation");

  // Room calendar: find the day with the booking
  await dr.goto(`${BASE}/ot`);
  await dr.waitForSelector(".ot-cal");
  for (let i = 0; i < 10 && !(await dr.locator(".ot-booking").count()); i++) {
    await dr.click("button[aria-label='Next day']");
    await dr.waitForTimeout(700);
  }
  await dr.waitForSelector(".ot-booking");
  await dr.locator(".ot-booking").first().click();
  await dr.waitForSelector("text=Cancel booking");
  await dr.screenshot({ path: join(shots, "10-ot-calendar.png"), fullPage: true });
  log("room calendar shows the booking");

  // Records: the finished consultation left the queue but stays on record with its chat and history
  await dr.goto(`${BASE}/doctor?tab=records`);
  await dr.waitForSelector(".queue li a .rec-completed");
  await dr.locator(".queue li a:has(.rec-completed)").first().click();
  await dr.waitForSelector("#hist-h");
  await dr.waitForSelector(".mini-transcript .msg.user");
  await dr.waitForTimeout(800);
  await dr.screenshot({ path: join(shots, "10b-records.png"), fullPage: true });
  log("records keep the finished consultation: chat, report, decisions");

  // Hand-over path: client asks for a person
  await op.goto(`${BASE}/`);
  await op.check("text=I agree to talk with an AI assistant");
  await op.check("text=I agree that the conversation");
  await op.click("button:has-text('Start consultation')");
  await op.waitForSelector(".msg.assistant");
  await say(op, "9811122233");
  await say(op, "123456");
  await say(op, "I'd like to talk to a doctor please");
  await op.waitForSelector(".banner.handed");
  await op.screenshot({ path: join(shots, "11-handed-over.png") });
  log("hand-over shown to client");
  await dr.goto(`${BASE}/doctor`);
  await dr.waitForSelector(".queue a.esc");
  await dr.locator(".queue a.esc").last().click(); // newest hand-over
  await dr.waitForSelector("text=Join as");
  await dr.click("button:has-text('Join as')");
  await dr.fill("input[aria-label='Message the client']", "Hello Rohan, I'm Dr Rao. How can I help?");
  await dr.click("form.send-row button");
  await dr.waitForTimeout(1000);
  await dr.screenshot({ path: join(shots, "12-doctor-joined.png"), fullPage: true });
  await op.waitForSelector(".msg.doctor", { timeout: 15000 });
  log("doctor message reached the client");

  // Phone width
  const mob = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
  const mp = await login(mob, "operator@clinic.local");
  await mp.screenshot({ path: join(shots, "13-mobile-consent.png"), fullPage: true });
  const mdr = await login(await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true }), "doctor@clinic.local");
  await mdr.goto(`${BASE}/ot`);
  await mdr.waitForSelector(".ot-cal");
  await mdr.waitForTimeout(600);
  const overflow = await mdr.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`room calendar scrolls sideways on a phone by ${overflow}px`);
  await mdr.screenshot({ path: join(shots, "14-mobile-ot.png"), fullPage: true });
  log("mobile layout captured");
  console.log("[e2e] PASS");
} catch (e) {
  console.error("[e2e] FAIL", e);
  process.exitCode = 1;
} finally {
  await browser.close();
}
