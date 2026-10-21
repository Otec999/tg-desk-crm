/**
 * Focused check of the admin SLA tab: opening the panel must not blow up and the
 * supervisor must see the policy read-only. Writes its own screenshots.
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: !!cond, label, detail });
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${cond ? "" : `  -> ${detail}`}`);
}

async function login(page, user, pass) {
  await page.goto(`${APP}/admin`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  // The admin login form renders two bare inputs: username ("admin") and a masked
  // password field. Neither carries a `type` attribute, so key off the placeholders.
  const userInput = page.locator('input[placeholder="admin"]').first();
  await userInput.waitFor({ state: "visible", timeout: 25000 });
  await userInput.fill(user);
  const passInput = page.locator('input[placeholder="••••••••"]').first();
  await passInput.fill(pass);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(4500);
  if (!page.url().includes("/admin/panel")) {
    await page.goto(`${APP}/admin/panel`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(4500);
  }
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });

  // ---------- admin: full control ----------
  await login(page, "admin", "admin123");
  check(page.url().includes("/admin/panel"), "admin reached the panel", page.url());

  const slaTab = page.getByRole("button", { name: "/^SLA$/i" }).first();
  const tabByName = page.locator("button", { hasText: /^SLA$/ }).first();
  const tab = (await slaTab.count()) ? slaTab : tabByName;
  check((await tab.count()) > 0, "SLA tab exists");
  if ((await tab.count()) > 0) {
    await tab.click();
    await page.waitForTimeout(2500);
  }

  const switches = page.locator('[role="switch"]');
  const switchCount = await switches.count();
  check(switchCount >= 2, `two SLA switches rendered (found ${switchCount})`);

  const target = page.locator('input[aria-label="Цель ответа, секунд"]');
  check((await target.count()) > 0, "target input rendered");
  if ((await target.count()) > 0) {
    const value = await target.first().inputValue();
    check(/^\d+$/.test(value), `target input holds a number (${value})`);
    check(!(await target.first().isDisabled()), "admin can edit the target");
  }

  const saveBtn = page.locator("button", { hasText: /Сохранить/ }).first();
  check((await saveBtn.count()) > 0, "save button rendered");
  if ((await saveBtn.count()) > 0) {
    check(await saveBtn.isDisabled(), "save is disabled while nothing changed");
  }

  await page.screenshot({ path: "ui-shots/sla-admin.png" });

  // ---------- supervisor: read-only ----------
  const sup = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const supErrors = [];
  sup.on("console", (m) => {
    if (m.type() === "error") supErrors.push(m.text());
  });
  await login(sup, "supervisor", "admin123");
  check(sup.url().includes("/admin/panel"), "supervisor reached the panel", sup.url());

  const supTab = sup.locator("button", { hasText: /^SLA$/ }).first();
  check((await supTab.count()) > 0, "supervisor sees the SLA tab");
  if ((await supTab.count()) > 0) {
    await supTab.click();
    await sup.waitForTimeout(2500);
  }

  const supTarget = sup.locator('input[aria-label="Цель ответа, секунд"]');
  const supVisible = (await supTarget.count()) > 0;
  check(supVisible, "supervisor sees the SLA policy values");
  if (supVisible) {
    const v = await supTarget.first().inputValue();
    check(/^\d+$/.test(v), `supervisor reads a real value (${v})`);
    check(await supTarget.first().isDisabled(), "supervisor cannot edit the target");
  }

  const supSave = sup.locator("button", { hasText: /Сохранить/ }).first();
  if ((await supSave.count()) > 0) {
    check(await supSave.isDisabled(), "supervisor cannot save");
  }

  await sup.screenshot({ path: "ui-shots/sla-supervisor.png" });

  check(consoleErrors.length === 0, "no console errors (admin)", consoleErrors.slice(0, 2).join(" | "));
  check(supErrors.length === 0, "no console errors (supervisor)", supErrors.slice(0, 2).join(" | "));

  await browser.close();

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n=== ${passed}/${results.length} SLA tab checks passed ===`);
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
