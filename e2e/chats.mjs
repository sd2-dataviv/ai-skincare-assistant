// End-to-end check of follow-up chats in a real browser: a returning client asks about a past consultation on the
// clinic device, a doctor starts a chat from the Messages tab, the client reads and replies on a phone (/my).
// Prereqs: API on :8000 and web on :5173 (any LLM setting), the demo client 98765 43210 with at least one past visit.
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
const STAMP = new Date().toISOString().slice(11, 19);
let step = 0;
const log = (m) => console.log(`[chats ${String(++step).padStart(2, "0")}] ${m}`);
const noSideScroll = async (page, what) => {
  const o = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (o > 1) throw new Error(`${what} scrolls sideways on a phone by ${o}px`);
};

const browser = await chromium.launch({ executablePath: CHROME, headless: true });

async function login(ctx, email) {
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login`);
  await page.fill("#email", email);
  await page.fill("#password", PW);
  await page.click("button:has-text('Sign in')");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  return page;
}

try {
  // 1. Clinic device: the returning client asks about a past consultation from the History tab
  const op = await login(await browser.newContext({ viewport: { width: 1366, height: 900 } }), "operator@clinic.local");
  await op.check("text=I agree to talk with an AI assistant");
  await op.check("text=I agree that the conversation");
  await op.click("button:has-text('Start consultation')");
  await op.waitForSelector(".msg.assistant");
  for (const t of ["9876543210", "123456"]) {
    await op.fill("#say", t);
    await op.click("form.send-row button");
    await op.waitForFunction(() => !document.querySelector("#say")?.disabled, null, { timeout: 30000 });
  }
  await op.click("button[role=tab]:has-text('History')");
  await op.waitForSelector(".past-visit");
  if (await op.locator("#say").count()) throw new Error("the consultation composer still shows on the History tab");
  log("clinic device: History tab open, consultation composer hidden");
  await op.locator(".past-visit").first().locator("button:has-text('Ask about this visit'), button:has-text('Open chat')").click();
  await op.waitForSelector(".thread-chat");
  const before = await op.locator(".thread-chat .msg.assistant").count();
  await op.fill(".thread-chat input[type=text]", "What's my plan again?");
  await op.click(".thread-chat form.send-row button");
  await op.waitForFunction((n) => document.querySelectorAll(".thread-chat .msg.assistant").length > n, before, { timeout: 45000 });
  const answer = await op.locator(".thread-chat .msg.assistant .bubble").last().innerText();
  log("assistant answered about the past visit: " + answer.replace(/\s+/g, " ").slice(0, 90));
  await op.screenshot({ path: join(shots, "c1-kiosk-visit-chat.png") });
  await op.click("button:has-text('All consultations')");
  await op.click("button[role=tab]:has-text('Conversation')");

  // 2. Doctor starts a chat with the client from the Messages tab
  const dr = await login(await browser.newContext({ viewport: { width: 1440, height: 950 } }), "doctor@clinic.local");
  await dr.goto(`${BASE}/doctor?tab=messages`);
  await dr.click("button:has-text('New message')");
  await dr.fill(".records-filters input", "98765");
  await dr.locator(".pick-row").first().click();
  await dr.waitForSelector(".detail-head h1");
  const msg = `Hello from the clinic (${STAMP}). How is your skin feeling this week?`;
  await dr.fill(".detail .send-row input", msg);
  await dr.click(".detail .send-row button");
  await dr.waitForSelector(`.mini-transcript .bubble:has-text("${STAMP}")`);
  await dr.waitForSelector("text=Sent");
  log("doctor sent a message: " + (await dr.locator("[role=status]").innerText()));
  await dr.screenshot({ path: join(shots, "c2-doctor-messages.png") });

  // 3. The clinic device shows it as new on the History tab
  await op.waitForSelector("button[role=tab]:has-text('new')", { timeout: 20000 });
  log("clinic device: History tab shows " + (await op.locator("button[role=tab]:has-text('History')").innerText()).replace(/\s+/g, " "));

  // 4. The client reads and replies on their phone
  const ph = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true })).newPage();
  await ph.goto(`${BASE}/my`);
  await ph.fill("#m", "9876543210");
  await ph.click("button:has-text('Send me a code')");
  await ph.fill("#c", "123456");
  await ph.click("button:has-text('Sign in')");
  await ph.waitForSelector(".thread-row");
  await noSideScroll(ph, "/my");
  await ph.screenshot({ path: join(shots, "c3-phone-home.png"), fullPage: true });
  log("phone: signed in with mobile + code; " + (await ph.locator(".thread-row.unread").count()) + " chat(s) with new messages");
  await ph.locator(".thread-row.unread").first().click();
  await ph.waitForSelector(`.thread-chat .bubble:has-text("${STAMP}")`);
  await ph.fill(".thread-chat input[type=text]", `Much better, thank you (${STAMP})`);
  await ph.click(".thread-chat form.send-row button");
  await ph.waitForSelector(`.thread-chat .msg.user .bubble:has-text("Much better")`);
  await noSideScroll(ph, "the phone chat");
  await ph.screenshot({ path: join(shots, "c4-phone-chat.png") });
  log("phone: read the doctor's message and replied");

  // 5. The doctor sees the reply arrive
  await dr.waitForSelector(`.mini-transcript .msg.user:has-text("Much better, thank you (${STAMP})")`, { timeout: 20000 });
  await dr.screenshot({ path: join(shots, "c5-doctor-sees-reply.png") });
  log("doctor: the client's reply arrived");

  // 6. Phone layout of the staff screen isn't the point, but the client's summary page must fit a phone too
  await ph.click("button:has-text('All consultations')");
  const sum = ph.locator("a:has-text('View the full summary')").first();
  if (await sum.count()) {
    await sum.click();
    await ph.waitForSelector(".letter");
    await noSideScroll(ph, "the summary on /my");
    log("phone: past visit summary opens and fits the screen");
  }
  console.log("CHATS E2E PASS");
} catch (e) {
  console.error("CHATS E2E FAIL:", e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
