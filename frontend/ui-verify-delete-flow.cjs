/**
 * Click the real delete button in the UI and verify the full contract:
 * the message disappears from Telegram, stays in the CRM as a grey trace,
 * and the API reports deleted_source === "CRM".
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: !!cond, label });
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${cond ? "" : `  -> ${detail}`}`);
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(6000);

  const chat = page.locator("text=Merchant Alpha - Payouts").first();
  if (await chat.count()) {
    await chat.click();
    await page.waitForTimeout(3500);
  }

  // Pick the last live (not yet deleted) bubble.
  const candidates = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('[id^="msg-"]').forEach((n) => {
      const hasDelete = Array.from(n.querySelectorAll("button")).some(
        (b) => b.getAttribute("aria-label") === "Удалить сообщение",
      );
      if (hasDelete) out.push(n.id);
    });
    return out;
  });
  console.log(`  bubbles offering "Удалить сообщение": ${candidates.length}`);
  check(candidates.length > 0, "delete button is available to op1");

  if (!candidates.length) {
    await browser.close();
    return 1;
  }

  const targetId = candidates[candidates.length - 1];
  const numericId = Number(targetId.replace("msg-", ""));
  console.log(`  target: ${targetId}`);

  const bubble = page.locator(`#${targetId}`);
  await bubble.scrollIntoViewIfNeeded();
  await bubble.hover({ force: true });
  await page.waitForTimeout(300);

  // First click arms the button, second confirms (inline two-step, no window.confirm).
  let del = bubble.locator('button[aria-label="Удалить сообщение"]').first();
  if ((await del.count()) === 0) {
    del = bubble.locator('button[aria-label*="Подтверд"]').first();
  }
  check((await del.count()) > 0, "delete control reachable after hover");
  if ((await del.count()) === 0) {
    await browser.close();
    return 1;
  }

  const deleteResponse = page
    .waitForResponse((r) => r.url().includes(`/api/messages/${numericId}`) && r.request().method() === "DELETE", {
      timeout: 20000,
    })
    .catch(() => null);

  await del.click({ force: true });
  await page.waitForTimeout(500);

  const armed = bubble.locator('button[aria-label*="Подтверд"]').first();
  if ((await armed.count()) > 0) {
    console.log("  armed -> confirming");
    await armed.click({ force: true });
  }

  const resp = await deleteResponse;
  check(resp !== null, "DELETE request fired from the UI");
  if (resp) {
    const status = resp.status();
    check(status === 200, `DELETE answered 200 (got ${status})`);
    try {
      const body = await resp.json();
      check(body.deleted_source === "CRM", `deleted_source is CRM (got ${body.deleted_source})`);
      check(Boolean(body.telegram?.requested), "Telegram deletion was requested");
      console.log(`  telegram: ${JSON.stringify(body.telegram)}`);
    } catch (e) {
      check(false, "DELETE body parsed", String(e));
    }
  }

  await page.waitForTimeout(2500);

  // The row must remain in the CRM, rendered as a grey trace.
  const stillThere = page.locator(`#${targetId}`);
  check((await stillThere.count()) > 0, "message STAYS in the CRM after deletion");

  if ((await stillThere.count()) > 0) {
    const text = (await stillThere.first().innerText()).replace(/\s+/g, " ").trim();
    console.log(`  bubble text: ${text.slice(0, 120)}`);
    check(/Удалено/i.test(text), "bubble shows a deletion label");
    const grey = await stillThere.first().evaluate((n) => {
      const el = n.querySelector(".bubble-deleted") || n;
      const cs = getComputedStyle(el);
      const m = cs.backgroundColor.match(/\d+/g);
      if (!m) return null;
      const [r, g, b] = m.map(Number);
      return { r, g, b, isGrey: Math.abs(r - g) < 18 && Math.abs(g - b) < 18 };
    });
    console.log(`  background: ${JSON.stringify(grey)}`);
    check(grey?.isGrey === true, "trace is grey, not red");
    check(
      !(await stillThere.locator('button[aria-label="Удалить сообщение"]').count()),
      "delete button is gone for an already-deleted message",
    );
  }

  await page.screenshot({ path: "ui-shots/delete-verified.png" });
  check(errors.length === 0, "no console errors", errors.slice(0, 2).join(" | "));

  await browser.close();

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n=== ${passed}/${results.length} delete-flow checks passed ===`);
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
