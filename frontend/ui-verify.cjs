/**
 * Browser-level UI verification for the CONTRACTS_V2 feature set.
 *
 * Drives the real Vite app against the live backend: login, media rendering,
 * reactions, file picker and the separate admin panel. Screenshots land in
 * `frontend/ui-shots/` for visual review.
 */

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const APP = "http://127.0.0.1:5173";
const SHOTS = path.join(__dirname, "ui-shots");

const results = [];
function check(cond, label, detail = "") {
  results.push({ ok: Boolean(cond), label });
  console.log(`${cond ? "[PASS]" : "[FAIL]"} ${label}${!cond && detail ? `  -> ${detail}` : ""}`);
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  const consoleErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err.message}`));

  // ------------------------------------------------------------ 1. login
  await page.goto(APP, { waitUntil: "networkidle" });
  await page.screenshot({ path: path.join(SHOTS, "01-login.png") });

  const usernameField = page.locator('input[placeholder="op1"]').first();
  await usernameField.fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(3500);

  const loggedIn = await page.locator("textarea").count();
  check(loggedIn > 0, "ui: operator logged in (composer visible)");
  if (loggedIn === 0) {
    await page.screenshot({ path: path.join(SHOTS, "01b-login-failed.png") });
    console.log("console errors:", consoleErrors.slice(0, 5));
    await browser.close();
    return 1;
  }
  await page.screenshot({ path: path.join(SHOTS, "02-crm.png") });

  // -------------------------------------------------- 2. pick the Telegram chat
  const chatSearch = page.locator('input[placeholder*="оиск"], input[type="search"]').first();
  if (await chatSearch.count()) {
    await chatSearch.fill("Merchant Alpha");
    await page.waitForTimeout(1200);
  }
  const chatRow = page.locator("text=Merchant Alpha").first();
  if (await chatRow.count()) {
    await chatRow.click();
    await page.waitForTimeout(2500);
  }
  await page.screenshot({ path: path.join(SHOTS, "03-chat.png") });

  // ------------------------------------------------------- 3. media bubbles
  const images = page.locator('img[src*="/api/media/"]');
  const imageCount = await images.count();
  check(imageCount > 0, "ui: at least one media image rendered", `found ${imageCount}`);
  if (imageCount > 0) {
    const src = await images.first().getAttribute("src");
    check(src.includes("token="), "ui: <img> carries the ?token= fallback", src);
    const naturalWidth = await images.first().evaluate((el) => el.naturalWidth);
    check(naturalWidth > 0, "ui: image actually decoded (not a broken 401)", `naturalWidth=${naturalWidth}`);
  }
  await page.screenshot({ path: path.join(SHOTS, "04-media.png") });

  // ---------------------------------------------------------- 4. reactions
  const chips = page.locator('button[aria-pressed]');
  const chipCount = await chips.count();
  check(chipCount > 0, "ui: existing reactions rendered", `found ${chipCount}`);

  const reactionButtons = page.locator('button[aria-label="Поставить реакцию"]');
  const pickerCount = await reactionButtons.count();
  check(pickerCount > 0, "ui: reaction picker button present", `found ${pickerCount}`);

  if (pickerCount > 0) {
    const bubble = reactionButtons.first();
    await bubble.scrollIntoViewIfNeeded();
    await bubble.click({ force: true });
    await page.waitForTimeout(800);
    const emojiButtons = page.locator('button[aria-label="Нравится"], button[aria-label="Огонь"]');
    const emojiCount = await emojiButtons.count();
    check(emojiCount > 0, "ui: emoji picker opened", `found ${emojiCount}`);
    await page.screenshot({ path: path.join(SHOTS, "05-reaction-picker.png") });

    if (emojiCount > 0) {
      await emojiButtons.first().click();
      await page.waitForTimeout(1800);
      const mineChips = page.locator('button[aria-pressed="true"]');
      const mineCount = await mineChips.count();
      check(mineCount > 0, "ui: my reaction shows as selected", `found ${mineCount}`);
      await page.screenshot({ path: path.join(SHOTS, "06-reaction-set.png") });
    }
  }

  // -------------------------------------------------------- 5. file picker
  const paperclip = page.locator('button[aria-label="Прикрепить файл"], button[title="Прикрепить файл"]');
  const clipCount = await paperclip.count();
  check(clipCount > 0, "ui: file picker (paperclip) present", `found ${clipCount}`);

  if (clipCount > 0) {
    const chooser = page.waitForEvent("filechooser", { timeout: 5000 }).catch(() => null);
    await paperclip.first().click({ force: true });
    const fileChooser = await chooser;
    check(Boolean(fileChooser), "ui: clicking paperclip opens a file dialog");
    if (fileChooser) {
      const sample = path.join(SHOTS, "_upload-sample.txt");
      fs.writeFileSync(sample, "проверка загрузки из UI");
      await fileChooser.setFiles(sample);
      await page.waitForTimeout(1200);
      await page.screenshot({ path: path.join(SHOTS, "07-file-attached.png") });
      const bodyText = await page.locator("body").innerText();
      check(
        bodyText.includes("_upload-sample") || bodyText.includes("Б"),
        "ui: attached file preview shown before sending",
      );
    }
  }

  // -------------------------------------------------------- 6. lightbox
  await page.keyboard.press("Escape");

  // ------------------------------------------ 7. admin panel (separate form)
  const adminPage = await context.newPage();
  adminPage.on("pageerror", (err) => consoleErrors.push(`admin pageerror: ${err.message}`));
  await adminPage.goto(`${APP}/admin`, { waitUntil: "networkidle" });
  await adminPage.waitForTimeout(1500);
  await adminPage.screenshot({ path: path.join(SHOTS, "08-admin-login.png") });

  const adminText = await adminPage.locator("body").innerText();
  check(
    adminText.includes("Административная панель") || adminText.includes("Администр"),
    "admin: separate login form rendered at /admin",
  );
  check(
    adminPage.url().includes("/admin"),
    "admin: /admin route served (not the CRM)",
    adminPage.url(),
  );

  // Log in as the administrator.
  const adminUser = adminPage.locator('input[placeholder="admin"], input:not([type="password"])').first();
  await adminUser.fill("admin");
  await adminPage.locator('input[type="password"]').first().fill("admin123");
  await adminPage.keyboard.press("Enter");
  await adminPage.waitForTimeout(4000);
  await adminPage.screenshot({ path: path.join(SHOTS, "09-admin-panel.png") });

  const panelText = await adminPage.locator("body").innerText();
  check(
    panelText.includes("Администрирование") || panelText.includes("Оператор"),
    "admin: panel opened after login",
    panelText.slice(0, 120),
  );
  check(panelText.includes("op1") || panelText.includes("supervisor") || panelText.includes("admin"),
    "admin: operator list rendered");
  check(panelText.includes("Telegram") || panelText.includes("аккаунт"), "admin: accounts tab present");
  check(panelText.includes("Журнал") || panelText.includes("действий"), "admin: audit tab present");

  await adminPage.screenshot({ path: path.join(SHOTS, "10-admin-full.png"), fullPage: true });

  // --------------------------------------------------------------- summary
  const realErrors = consoleErrors.filter(
    (e) => !e.includes("favicon") && !e.includes("Download the React DevTools"),
  );
  check(realErrors.length === 0, "ui: no console errors", realErrors.slice(0, 3).join(" | "));

  await browser.close();

  const failed = results.filter((r) => !r.ok);
  console.log(`\n=== ${results.length - failed.length}/${results.length} UI checks passed ===`);
  if (failed.length) console.log("FAILED: " + failed.map((f) => f.label).join("; "));
  return failed.length ? 1 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error("FATAL:", err.message);
    process.exit(1);
  });
