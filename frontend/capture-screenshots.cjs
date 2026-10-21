/**
 * Produce the screenshots embedded in README.md.
 *
 * Each shot is captured at a fixed viewport with a device scale factor of 2 so the
 * PNG stays crisp on GitHub's retina rendering. The script asserts that the content it
 * is about to photograph is actually present — a blank panel must fail loudly instead
 * of shipping an empty image.
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const OUT = path.resolve(__dirname, "..", "docs", "screenshots");

const results = [];
function check(cond, label, detail = "") {
  results.push({ ok: !!cond, label });
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${cond ? "" : `  -> ${detail}`}`);
}

async function login(page, user, pass) {
  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill(user);
  await page.locator('input[type="password"]').first().fill(pass);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(6500);
}

/** Screenshot the whole viewport, after waiting for fonts and pending images. */
async function shot(page, name) {
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
    await Promise.all(
      Array.from(document.images)
        .filter((img) => !img.complete)
        .map((img) => new Promise((r) => {
          img.onload = r;
          img.onerror = r;
          setTimeout(r, 3000);
        })),
    );
  });
  await page.waitForTimeout(700);
  const file = path.join(OUT, name);
  await page.screenshot({ path: file, fullPage: false });
  const bytes = fs.statSync(file).size;
  check(bytes > 20000, `${name} written (${Math.round(bytes / 1024)} KB)`, `${bytes} bytes`);
  return file;
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();

  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  // ---------------------------------------------------------------- 1. login
  await page.goto(APP, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const hasLogin = (await page.locator('input[type="password"]').count()) > 0;
  check(hasLogin, "login form rendered");
  await shot(page, "01-login.png");

  // --------------------------------------------------------------- 2. console
  await login(page, "op1", "operator123");
  const chatCount = await page.locator("text=Merchant Alpha - Payouts").count();
  check(chatCount > 0, "chat list populated");

  await page.locator("text=Merchant Alpha - Payouts").first().click();
  await page.waitForTimeout(4000);
  const bubbles = await page.locator('[id^="msg-"]').count();
  check(bubbles > 3, `timeline rendered (${bubbles} messages)`);
  await shot(page, "02-operator-desk.png");

  // --------------------------------------------- 3. reactions on a bubble
  const bubble = page.locator('[id^="msg-"]').nth(Math.max(0, bubbles - 3));
  await bubble.hover();
  await page.waitForTimeout(800);
  /* Exact labels from ReactionBar.tsx: the hover trigger opens the emoji panel. */
  const reactTrigger = page.locator('[aria-label="Поставить реакцию"]').first();
  if ((await reactTrigger.count()) > 0) {
    await reactTrigger.click();
    await page.waitForTimeout(1000);
    const picker = page.locator('[aria-label="Выбор реакции"]').first();
    const opened = (await picker.count()) > 0 && (await picker.isVisible());
    check(opened, "reaction picker opened");
    await shot(page, "03-reactions.png");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
  } else {
    check(false, "reaction trigger found");
  }

  // ------------------------------------------------- 4. global search (Ctrl+K)
  await page.keyboard.press("Control+k");
  await page.waitForTimeout(700);
  const searchInput = page.locator('input[placeholder*="оиск"], input[placeholder*="earch"]').first();
  const hasSearch = (await searchInput.count()) > 0;
  check(hasSearch, "global search overlay opened");
  if (hasSearch) {
    await searchInput.fill("payout");
    await page.waitForTimeout(2200);
    const hits = await page.locator('[data-testid*="result"], [role="option"]').count();
    check(hits > 0, `search returned hits (${hits})`);
    await shot(page, "04-search.png");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
  }

  // ------------------------------------------- 5. scroll affordances visible
  await page.mouse.move(800, 500);
  await page.mouse.wheel(0, -2600);
  await page.waitForTimeout(900);
  const upBtn = await page.locator('[data-testid="scroll-to-top"]').count();
  check(upBtn > 0, "scroll buttons visible after scrolling up");
  await shot(page, "05-scroll.png");

  await ctx.close();

  // ------------------------------------------------------- 6. supervisor SLA
  const adminCtx = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    deviceScaleFactor: 2,
  });
  const adminPage = await adminCtx.newPage();
  adminPage.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await login(adminPage, "supervisor", "admin123");
  await adminPage.waitForTimeout(1500);
  const slaLabels = await adminPage.locator("text=/SLA/i").count();
  check(slaLabels > 0, "SLA settings reachable for supervisor");
  await shot(adminPage, "06-sla-settings.png");

  await adminCtx.close();

  check(errors.length === 0, "no console errors while capturing", errors.slice(0, 2).join(" | "));
  await browser.close();

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n=== ${passed}/${results.length} screenshot checks passed ===`);
  console.log(`output: ${OUT}`);
  const failed = results.filter((r) => !r.ok).map((r) => r.label);
  if (failed.length) console.log("FAILED: " + failed.join("; "));
  return failed.length ? 1 : 0;
}

main()
  .then((c) => process.exit(c))
  .catch((e) => {
    console.error("FATAL:", e.message);
    process.exit(1);
  });
