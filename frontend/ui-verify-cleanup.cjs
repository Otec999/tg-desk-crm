/** Confirm the CRM shows exactly the two real chats after cleanup + restart. */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: !!cond, label });
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${cond ? "" : `  -> ${detail}`}`);
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  let chatsPayload = null;
  page.on("response", async (r) => {
    if (/\/api\/chats(\?|$)/.test(r.url()) && r.request().method() === "GET") {
      try {
        chatsPayload = await r.json();
      } catch {
        /* ignore */
      }
    }
  });

  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(6000);

  const items = Array.isArray(chatsPayload?.items)
    ? chatsPayload.items
    : Array.isArray(chatsPayload)
      ? chatsPayload
      : [];
  console.log(`  chats from the API: ${items.length}`);
  items.forEach((c) => console.log(`    #${c.id} ${c.title} (${c.tg_chat_id})`));

  check(items.length === 2, `exactly 2 chats remain (got ${items.length})`);
  const ids = items.map((c) => String(c.tg_chat_id)).sort();
  check(
    JSON.stringify(ids) === JSON.stringify(["-1004338873193", "-1004395719918"].sort()),
    `both are the real monitored chats (${ids.join(", ")})`,
  );

  // Rendered list must match, and opening a chat still works.
  const rendered = await page.evaluate(() =>
    Array.from(document.querySelectorAll("aside, nav, [data-testid]"))
      .map((n) => n.textContent || "")
      .join(" ")
      .includes("Merchant"),
  );
  check(rendered, "chat list renders merchant chats");

  const alpha = page.locator("text=Merchant Alpha - Payouts").first();
  check((await alpha.count()) > 0, "В«Merchant Alpha - PayoutsВ» is listed");
  await alpha.click();
  await page.waitForTimeout(3500);

  const bubbles = await page.locator('[id^="msg-"]').count();
  console.log(`  bubbles after opening: ${bubbles}`);
  check(bubbles > 0, `timeline loads (${bubbles} messages)`);

  const scrolled = await page.evaluate(() => {
    const b = document.querySelector('[id^="msg-"]');
    let el = b?.parentElement ?? null;
    while (el) {
      const cs = getComputedStyle(el);
      if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) break;
      el = el.parentElement;
    }
    if (!el) return null;
    return Math.round(el.scrollHeight - el.scrollTop - el.clientHeight);
  });
  check(scrolled !== null && scrolled <= 20, `opens pinned to the newest message (${scrolled}px)`);

  check(errors.length === 0, "no console errors", errors.slice(0, 2).join(" | "));

  await page.screenshot({ path: "ui-shots/after-cleanup.png" });
  await browser.close();

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n=== ${passed}/${results.length} checks passed ===`);
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

