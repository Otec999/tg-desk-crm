/**
 * Verify the timeline scroll affordances (and auto-scroll on chat open):
 *   1. opening a chat lands at the newest message;
 *   2. scrolling up reveals the "up" button and the "down" button;
 *   3. the "down" button returns to the newest message;
 *   4. the "up" button returns to the top.
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: !!cond, label });
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${cond ? "" : `  -> ${detail}`}`);
}

const metrics = (page) =>
  page.evaluate(() => {
    // The scrolling container is the one with overflow-y auto/scroll that holds bubbles.
    const bubble = document.querySelector('[id^="msg-"]');
    let el = bubble?.parentElement ?? null;
    while (el) {
      const cs = getComputedStyle(el);
      if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) break;
      el = el.parentElement;
    }
    if (!el) return null;
    return {
      top: Math.round(el.scrollTop),
      height: Math.round(el.scrollHeight),
      client: Math.round(el.clientHeight),
      distance: Math.round(el.scrollHeight - el.scrollTop - el.clientHeight),
    };
  });

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(6000);

  // ---------- 1. auto-scroll on open ----------
  const chat = page.locator("text=Merchant Alpha - Payouts").first();
  check((await chat.count()) > 0, "chat present in the list");
  await chat.click();
  await page.waitForTimeout(3500);

  const opened = await metrics(page);
  console.log(`  after open: ${JSON.stringify(opened)}`);
  check(opened !== null, "timeline measured");
  check(opened && opened.distance <= 20, `opened at the bottom (distance=${opened?.distance})`);

  // ---------- 2. scrolling up reveals both buttons ----------
  await page.mouse.move(720, 400);
  await page.mouse.wheel(0, -3000);
  await page.waitForTimeout(700);

  const scrolled = await metrics(page);
  console.log(`  after wheel up: ${JSON.stringify(scrolled)}`);
  check(scrolled && scrolled.top > 300, `timeline actually scrolled (top=${scrolled?.top})`);

  const upBtn = page.locator('[data-testid="scroll-to-top"]');
  const downBtn = page.locator('[data-testid="scroll-to-bottom"]');
  check((await upBtn.count()) > 0, "В«РІРІРµСЂС…В» button appears when scrolled down");
  check((await downBtn.count()) > 0, "В«РІРЅРёР·В» button appears when away from the bottom");
  check(await upBtn.first().isVisible(), "В«РІРІРµСЂС…В» is visible");
  check(await downBtn.first().isVisible(), "В«РІРЅРёР·В» is visible");

  // ---------- 3. the down button returns to the newest message ----------
  if ((await downBtn.count()) > 0) {
    await downBtn.first().click();
    await page.waitForTimeout(1200);
    const back = await metrics(page);
    console.log(`  after В«РІРЅРёР·В»: ${JSON.stringify(back)}`);
    check(back && back.distance <= 20, `В«РІРЅРёР·В» reached the bottom (distance=${back?.distance})`);
    check((await page.locator('[data-testid="scroll-to-bottom"]').count()) === 0, "В«РІРЅРёР·В» hides at the bottom");
    check((await page.locator('[data-testid="scroll-to-top"]').count()) === 0, "В«РІРІРµСЂС…В» hides at the bottom");
  }

  // ---------- 4. the up button returns to the top ----------
  await page.mouse.move(720, 400);
  await page.mouse.wheel(0, -3000);
  await page.waitForTimeout(700);

  const upAgain = page.locator('[data-testid="scroll-to-top"]');
  if ((await upAgain.count()) > 0) {
    await upAgain.first().click();
    await page.waitForTimeout(2600);
    const top = await metrics(page);
    console.log(`  after В«РІРІРµСЂС…В»: ${JSON.stringify(top)}`);
    check(top && top.top <= 40, `В«РІРІРµСЂС…В» reached the top (top=${top?.top})`);
    check((await page.locator('[data-testid="scroll-to-top"]').count()) === 0, "В«РІРІРµСЂС…В» hides at the top");
    check((await page.locator('[data-testid="scroll-to-bottom"]').count()) > 0, "В«РІРЅРёР·В» stays visible at the top");
  }

  // ---------- 5. switching chats re-pins to the bottom ----------
  const other = page.locator("text=Merchant Omega").first();
  if ((await other.count()) > 0) {
    await other.click();
    await page.waitForTimeout(3500);
    const swapped = await metrics(page);
    console.log(`  after chat switch: ${JSON.stringify(swapped)}`);
    check(swapped && swapped.distance <= 20, `switching chats re-pins to the bottom (distance=${swapped?.distance})`);
  }

  check(errors.length === 0, "no console errors", errors.slice(0, 2).join(" | "));

  await page.screenshot({ path: "ui-shots/scroll-buttons.png" });
  await browser.close();

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n=== ${passed}/${results.length} scroll checks passed ===`);
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


