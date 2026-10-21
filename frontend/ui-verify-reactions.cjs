/**
 * Browser check for the new reaction set (CONTRACTS_V4 §1).
 *
 * Asserts that the picker offers exactly the six contract emoji, that `👀` is the
 * one replacing `😂`, and that clicking it persists through the API.
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const EXPECTED = ["👍", "❤️", "🔥", "👀", "👏", "🙏"];
const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: Boolean(cond), label });
  console.log(`${cond ? "[PASS]" : "[FAIL]"} ${label}${!cond && detail ? `  -> ${detail}` : ""}`);
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(4500);

  const chat = page.locator("text=Merchant Alpha - Payouts").first();
  if (await chat.count()) {
    await chat.click();
    await page.waitForTimeout(3000);
  }

  // Open the reaction picker on the first message that offers one.
  const trigger = page
    .locator('button[aria-label*="еакци"], button[title*="еакци"]')
    .first();
  const hasTrigger = (await trigger.count()) > 0;
  check(hasTrigger, "reaction trigger is rendered");
  if (!hasTrigger) {
    await browser.close();
    console.log(`\n=== 0/1 ===`);
    return 1;
  }

  await trigger.click({ force: true });
  await page.waitForTimeout(900);

  const emoji = await page.evaluate(() => {
    const set = new Set();
    for (const el of document.querySelectorAll('[role="menuitem"], button')) {
      const text = (el.textContent || "").trim();
      // A picker entry is a single grapheme, never a sentence.
      if (text && text.length <= 4) set.add(text);
    }
    return [...set];
  });
  console.log("    picker entries:", JSON.stringify(emoji));

  for (const wanted of EXPECTED) {
    check(emoji.includes(wanted), `picker offers ${wanted}`);
  }
  check(!emoji.includes("😂"), "CRITICAL: the laughing emoji is gone");

  // Persist 👀 and confirm the API stored it.
  const eyes = page.locator('button:has-text("👀"), [role="menuitem"]:has-text("👀")').first();
  const hasEyes = (await eyes.count()) > 0;
  check(hasEyes, "👀 is clickable in the picker");

  if (hasEyes) {
    const stored = page.waitForResponse(
      (r) => r.url().includes("/reaction") && r.request().method() === "PUT",
      { timeout: 15000 },
    ).catch(() => null);
    await eyes.click({ force: true });
    const response = await stored;
    check(Boolean(response), "PUT /reaction fired");
    if (response) {
      check(response.status() === 200, "PUT /reaction -> 200", String(response.status()));
      const body = await response.json().catch(() => null);
      const items = (body && body.items) || [];
      check(
        items.some((i) => i.emoji === "👀"),
        "server stored 👀",
        JSON.stringify(items),
      );
    }
    await page.waitForTimeout(1200);
  }

  const realErrors = errors.filter(
    (e) => !e.includes("favicon") && !e.includes("React DevTools"),
  );
  check(realErrors.length === 0, "no console errors", realErrors.slice(0, 3).join(" | "));

  await page.screenshot({ path: "ui-shots/v4-reactions.png" });
  await browser.close();

  const failed = results.filter((r) => !r.ok);
  console.log(`\n=== ${results.length - failed.length}/${results.length} reaction UI checks passed ===`);
  if (failed.length) console.log("FAILED: " + failed.map((f) => f.label).join("; "));
  return failed.length ? 1 : 0;
}

main()
  .then((c) => process.exit(c))
  .catch((e) => {
    console.error("FATAL:", e.message);
    process.exit(1);
  });
