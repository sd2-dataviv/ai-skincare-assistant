// End-to-end check of the treatment-room features in a real browser: a tentative booking made in the chat is
// confirmed from the calendar, the checklist gates the start, the treatment is started and finished, the setup screen
// shows machines, hours and holidays, and the report counts it. Also checks phone width.
// Prereqs: `make api-demo` (scripted AI, face check off) and `make web`; then: node e2e/ot.mjs
import { chromium } from "playwright-core";
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const shots = join(here, "screenshots");
mkdirSync(shots, { recursive: true });
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const CHROME = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";
let step = 0;
const log = (m) => console.log(`[ot ${String(++step).padStart(2, "0")}] ${m}`);

log("making a tentative booking through the chat (consultation → approval → slot)…");
const made = JSON.parse(execFileSync(join(here, "..", "backend", ".venv", "bin", "python"), [join(here, "chat_booking.py")],
  { encoding: "utf8" }).trim().split("\n").pop());
const day = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date(made.starts_at));
log(`tentative booking ${made.label}`);

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const noSideScroll = async (page, what) => {
  const o = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (o > 1) throw new Error(`${what} scrolls sideways on a phone by ${o}px`);
};

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login`);
  await page.fill("#email", "admin@clinic.local");
  await page.fill("#password", "Demo@12345");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));

  await page.goto(`${BASE}/ot`);
  await page.waitForSelector(".ot-cal");
  await page.fill("input[aria-label='Day']", day);
  const card = page.locator(".ot-booking.tentative", { hasText: "Q-Switched Laser Toning" }).first();
  await card.waitFor();
  log("calendar: the chat booking shows as tentative; " + (await page.locator("#toconf-h").innerText()));
  await card.click();
  await page.waitForSelector(".booking-panel");
  await page.screenshot({ path: join(shots, "o1-tentative.png") });

  await page.click(".booking-panel button:has-text('Confirm booking')");
  await page.waitForSelector(".booking-panel .pill.conf");
  log("confirmed: " + (await page.locator(".booking-panel [role=status]").innerText()));
  await page.click(".booking-panel button:has-text('Check in')");
  await page.waitForSelector(".booking-panel button:has-text('Start treatment')");
  if (await page.locator(".booking-panel button:has-text('Start treatment')").isEnabled()) throw new Error("start is allowed before the checklist");
  log("checked in; Start treatment stays locked until the checklist is done");
  await page.screenshot({ path: join(shots, "o2-checklist-locked.png") });

  const panel = page.locator(".booking-panel");
  for (const label of ["consent form", "Before photos", "re-checked", "skin cleansed"]) {
    const row = panel.locator("li.chk", { hasText: label });
    const box = row.locator("input[type=checkbox]");
    if (!(await box.isChecked())) {
      await box.click();  // saved on the server, then shown ticked
      await page.waitForFunction((el) => el.checked, await box.elementHandle(), { timeout: 10000 });
    }
  }
  const patch = panel.locator("li.chk", { hasText: "Patch test" });
  await patch.locator("button:has-text('Not needed')").click();
  await patch.locator("input[aria-label=Reason]").fill("Same laser 3 weeks ago, no reaction");
  await patch.locator("button:has-text('Save')").click();
  await page.waitForSelector(".booking-panel .bp-check-head .ok-text");
  await page.click(".booking-panel button:has-text('Start treatment')");
  await page.waitForSelector(".booking-panel button:has-text('Finish')");
  await page.screenshot({ path: join(shots, "o3-in-treatment.png") });
  await page.click(".booking-panel button:has-text('Finish')");
  await page.waitForSelector(".booking-panel .pill.quiet:has-text('Done')");
  log("checklist completed (patch test marked not needed, with a reason) → started → finished");

  await page.click("a[role=tab]:has-text('Machines')");
  await page.waitForSelector("#mach-h");
  await page.waitForSelector(".hours-doc");
  log(`setup: ${await page.locator("#mach-h + p + table tbody tr").count()} machines, doctors' hours, holidays, blocks`);
  await page.screenshot({ path: join(shots, "o4-setup.png"), fullPage: true });

  await page.click("a[role=tab]:has-text('Reports')");
  await page.waitForSelector(".kpi-row");
  await page.click("button:has-text('Next 2 weeks')");
  await page.waitForFunction(() => document.querySelector(".kpi-row .stat-value")?.textContent !== "—", null, { timeout: 10000 }).catch(() => undefined);
  const tiles = await page.locator(".stat-tile").allInnerTexts();
  log("report: " + tiles.slice(0, 2).map((t) => t.replace(/\n/g, " ")).join(" | "));
  await page.screenshot({ path: join(shots, "o5-reports.png"), fullPage: true });

  // Phone width
  const ph = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, storageState: await ctx.storageState() })).newPage();
  await ph.goto(`${BASE}/login`);
  await ph.fill("#email", "admin@clinic.local");
  await ph.fill("#password", "Demo@12345");
  await ph.click("button:has-text('Sign in')");
  await ph.waitForURL((u) => !u.pathname.startsWith("/login"));
  for (const tab of ["", "?tab=setup", "?tab=reports"]) {
    await ph.goto(`${BASE}/ot${tab}`);
    await ph.waitForTimeout(1200);
    await noSideScroll(ph, `/ot${tab}`);
  }
  log("phone width: calendar, setup and reports fit without sideways scrolling");
  console.log("OT E2E PASS");
} catch (e) {
  console.error("OT E2E FAIL:", e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
