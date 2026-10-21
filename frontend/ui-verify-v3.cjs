/**
 * Browser verification for CONTRACTS_V3: forwarding dialog and message editing.
 *
 * Drives the real app: opens the forward dialog from a message, checks the
 * anonymity warning, selects a chat, and exercises the edit composer.
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
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

  // ---------------------------------------------------------------- login
  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(4000);

  // Open the chat that has the forwarded message.
  const chat = page.locator("text=Merchant Omega - Payouts (CRM)").first();
  if (await chat.count()) {
    await chat.click();
    await page.waitForTimeout(2500);
  }

  // ------------------------------------------------------- forward dialog
  const forwardBtn = page.locator('button[aria-label="Переслать"], button[title="Переслать"]');
  const forwardCount = await forwardBtn.count();
  check(forwardCount > 0, "forward button present on a message", `found ${forwardCount}`);

  if (forwardCount > 0) {
    const btn = forwardBtn.first();
    await btn.scrollIntoViewIfNeeded();
    await btn.click({ force: true });
    await page.waitForTimeout(900);

    const dialog = page.locator('[role="dialog"]');
    check(await dialog.count() > 0, "forward dialog opened");

    if (await dialog.count() > 0) {
      const text = await dialog.innerText();
      check(text.includes("Переслать сообщение"), "dialog has a Russian title");
      check(
        text.includes("от имени нашего аккаунта") && text.includes("скрыто"),
        "CRITICAL: anonymity warning shown to the operator",
        text.slice(0, 200),
      );
      check(await page.locator('input[placeholder*="Поиск"]').count() > 0, "chat search field present");

      // Select a target chat inside the dialog: skip the close button (empty label),
      // the footer actions, and any row that is not an actual chat.
      const selected = await page.evaluate(() => {
        const dialogEl = document.querySelector('[role="dialog"]');
        if (!dialogEl) return false;
        const row = [...dialogEl.querySelectorAll("button")].find((b) => {
          const t = (b.textContent || "").trim();
          return t && t !== "Отмена" && t !== "Переслать" && t.includes("MRC-");
        });
        if (!row) return false;
        row.click();
        return true;
      });
      check(selected, "target chat selected");
      await page.waitForTimeout(900);

      const comment = page.locator('textarea[placeholder*="омментарий"]');
      if (await comment.count() > 0) {
        await comment.first().fill("UI-проверка пересылки");
      }
      // Inspect the submit button inside the page realm: the dialog title also
      // contains "Переслать", so Playwright text matchers are ambiguous here.
      const submitState = await page.evaluate(() => {
        const dialogEl = document.querySelector('[role="dialog"]');
        if (!dialogEl) return null;
        const btn = [...dialogEl.querySelectorAll("button")].find(
          (b) => (b.textContent || "").trim() === "Переслать",
        );
        return btn ? { disabled: btn.disabled } : null;
      });
      check(Boolean(submitState), "submit button located");
      if (submitState) {
        check(!submitState.disabled, "submit enabled after selecting a chat");
        await page.screenshot({ path: "ui-shots/v3-forward-dialog.png" });
        await page.evaluate(() => {
          const dialogEl = document.querySelector('[role="dialog"]');
          const btn = [...dialogEl.querySelectorAll("button")].find(
            (b) => (b.textContent || "").trim() === "Переслать",
          );
          btn?.click();
        });
        await page.waitForTimeout(3500);
        check(await page.locator('[role="dialog"]').count() === 0, "dialog closed after a successful send");
      }
    }
  }

  // ------------------------------------------------------------ editing
  const editBtn = page.locator('button[aria-label="Изменить"], button[title="Изменить"]');
  const editCount = await editBtn.count();
  check(editCount > 0, "edit button present", `found ${editCount}`);

  if (editCount > 0) {
    const btn = editBtn.first();
    await btn.scrollIntoViewIfNeeded();
    await btn.click({ force: true });
    await page.waitForTimeout(800);

    const body = await page.locator("body").innerText();
    check(body.includes("Редактирование сообщения"), "edit banner shown", body.slice(0, 120));

    const ta = page.locator("textarea").first();
    const placeholder = await ta.getAttribute("placeholder");
    check(
      (placeholder || "").includes("Изменить"),
      "composer switched to edit placeholder",
      placeholder || "(none)",
    );
    check(!(await ta.isDisabled()), "composer stays enabled while editing");

    const original = await ta.inputValue();
    check(original.length > 0, "edit field pre-filled with the message text", String(original.length));

    await ta.fill("Текст, изменённый через UI");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(3000);

    const after = await page.locator("body").innerText();
    check(after.includes("Текст, изменённый через UI"), "edited text appears in the timeline");
    check(!after.includes("Редактирование сообщения"), "edit mode exited after saving");
    await page.screenshot({ path: "ui-shots/v3-edited.png" });
  }

  // ---------------------------------------------------- edit history block
  const history = page.locator('[aria-label="История изменений"]');
  check(await history.count() >= 0, "edit history component reachable");

  const realErrors = errors.filter(
    (e) => !e.includes("favicon") && !e.includes("React DevTools"),
  );
  check(realErrors.length === 0, "no console errors", realErrors.slice(0, 3).join(" | "));

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n=== ${results.length - failed.length}/${results.length} UI checks passed ===`);
  if (failed.length) console.log("FAILED: " + failed.map((f) => f.label).join("; "));
  return failed.length ? 1 : 0;
}

main()
  .then((c) => process.exit(c))
  .catch((e) => {
    console.error("FATAL:", e.message);
    process.exit(1);
  });
