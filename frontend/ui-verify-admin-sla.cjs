/**
 * Browser verification for CONTRACTS_V5 §2 — вкладка «SLA» в админке.
 *
 * Прогон против живого UI и живого бэкенда:
 *
 *   1. `admin` входит через реальную форму (`/admin` → `/admin/panel`).
 *   2. Вкладка «SLA» есть в переключателе и открывается.
 *   3. На вкладке два переключателя (`role=switch`) и числовой инпут
 *      «Цель ответа, секунд» (`inputMode=numeric`), значения пришли с сервера.
 *   4. «Сохранить» погашена, пока изменений нет, и оживает после правки.
 *   5. Переключение `sla_enabled` + сохранение → `PATCH` 200, тост
 *      «Настройки SLA сохранены», новое значение ушло на сервер.
 *   6. Перезагрузка страницы → вкладка показывает сохранённое значение
 *      (то есть состояние пришло из БД, а не из памяти стора).
 *   7. Возврат исходного значения.
 *   8. `SUPERVISOR` (учётка `supervisor`): вход в панель и проверка, что ни один
 *      контрол не кликается — SLA-переключатели, инпут и «Сохранить» погашены,
 *      как и «Создать оператора», «Изменить» и «Добавить аккаунт». Получить 403
 *      после клика оператор больше не может.
 *   9. Скриншот в `ui-shots/` + гарантированное восстановление `sla_enabled` в
 *      блоке `finally` (оборванный прогон не оставит стенд выключенным).
 *
 * Бэкенд на :8000 может перезапускаться параллельными агентами — сначала
 * `waitForBackend()` с ретраями, упавший запрос не «прощается».
 */

const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

const APP = "http://127.0.0.1:5173";
const API = "http://127.0.0.1:8000";
const ADMIN_USER = "admin";
const ADMIN_PASS = "admin123";
/** Сид-пароль супервизора = `backend_supervisor_password`. */
const SUPERVISOR_USER = "supervisor";
const SUPERVISOR_PASS = "admin123";
const SHOT = "ui-shots/admin-sla-tab.png";

const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: Boolean(cond), label, detail });
  console.log(`${cond ? "[PASS]" : "[FAIL]"} ${label}${!cond && detail ? `  -> ${detail}` : ""}`);
}

/** Ждём, пока бэкенд ответит (другой агент мог его перезапускать). */
async function waitForBackend(timeoutMs = 180_000) {
  const started = Date.now();
  for (;;) {
    try {
      const response = await fetch(`${API}/openapi.json`, { method: "GET" });
      if (response.ok) return true;
    } catch {
      /* ещё поднимается */
    }
    if (Date.now() - started > timeoutMs) return false;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

async function apiLogin(username, password) {
  const response = await fetch(`${API}/api/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(`admin login failed: HTTP ${response.status}`);
  return response.json();
}

async function apiGetSettings(token) {
  const response = await fetch(`${API}/api/admin/settings`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error(`GET settings failed: HTTP ${response.status}`);
  return response.json();
}

async function apiPatchSettings(token, payload) {
  const response = await fetch(`${API}/api/admin/settings`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`PATCH settings failed: HTTP ${response.status}`);
  return response.json();
}

/** Что реально разрешает сервер учётке SUPERVISOR на `/api/admin/settings`. */
async function probeSupervisorAccess() {
  const login = await fetch(`${API}/api/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: SUPERVISOR_USER, password: SUPERVISOR_PASS }),
  });
  if (!login.ok) return { getStatus: 0, patchStatus: 0 };
  const session = await login.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };

  const getResponse = await fetch(`${API}/api/admin/settings`, { headers });
  let current = null;
  if (getResponse.ok) current = (await getResponse.json()).sla_enabled;

  /* PATCH шлётся с текущим значением: даже если сервер пропустит, данные целы. */
  const patchResponse = await fetch(`${API}/api/admin/settings`, {
    method: "PATCH",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify(current === null ? {} : { sla_enabled: current }),
  });
  return { getStatus: getResponse.status, patchStatus: patchResponse.status };
}

/**
 * Вход через форму админки. Возвращает `true`, если панель открылась.
 *
 * Сессия гасится из `about:blank`: если чистить `localStorage` на уже
 * открытом `/admin/panel`, стор успевает уйти на `/admin` и следующий `goto`
 * падает с «interrupted by another navigation».
 */
async function loginAdmin(page, username = ADMIN_USER, password = ADMIN_PASS) {
  await page.goto("about:blank");
  await page.goto(`${APP}/admin`, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    window.localStorage.removeItem("tgdesk.admin_token");
    window.localStorage.removeItem("tgdesk.admin_operator");
  });
  await page.waitForTimeout(300);
  await page.goto(`${APP}/admin`, { waitUntil: "networkidle" });

  const user = page.locator('input[autocomplete="username"], input[placeholder*="Логин"]').first();
  await user.waitFor({ state: "visible", timeout: 30_000 });
  await user.fill(username);
  await page.locator('input[type="password"]').first().fill(password);
  await page.keyboard.press("Enter");

  try {
    await page.waitForFunction(() => window.location.pathname.startsWith("/admin/panel"), null, {
      timeout: 25_000,
    });
  } catch {
    return false;
  }
  await page.waitForTimeout(1200);
  return true;
}

async function openSlaTab(page) {
  const tab = page.getByRole("button", { name: "SLA", exact: true });
  await tab.waitFor({ state: "visible", timeout: 15_000 });
  await tab.click();
  await page.waitForTimeout(1500);
}

async function run() {
  if (!(await waitForBackend())) {
    console.error("Бэкенд на :8000 недоступен — проверка не выполнена.");
    process.exit(2);
  }

  fs.mkdirSync(path.dirname(SHOT), { recursive: true });

  const session = await apiLogin(ADMIN_USER, ADMIN_PASS);
  const initial = await apiGetSettings(session.access_token);

  /* Само-восстановление после оборванного прогона: дефолт §2.1 — `true`. */
  if (!initial.sla_enabled) {
    console.log("[recover] sla_enabled=false после прерванного прогона — возвращаю true");
    await apiPatchSettings(session.access_token, { sla_enabled: true });
  }
  const baseline = await apiGetSettings(session.access_token);
  console.log("Сервер до теста:", JSON.stringify(baseline));

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", (error) => console.log("[pageerror]", error.message));
  page.on("response", (response) => {
    if (response.url().includes("/api/admin/")) {
      console.log(`[http] ${response.status()} ${response.request().method()} ${response.url()}`);
    }
  });

  try {
    await loginAdmin(page);
    check(page.url().includes("/admin/panel"), "Вход admin/admin123 привёл на /admin/panel", page.url());

    const tabButtons = await page.locator("nav button").allInnerTexts();
    check(
      tabButtons.some((text) => text.trim() === "SLA"),
      "Вкладка «SLA» есть в переключателе",
      tabButtons.join(" | "),
    );

    await openSlaTab(page);

    const switchCount = await page.locator('[role="switch"]').count();
    check(switchCount >= 2, "На вкладке минимум два переключателя", `${switchCount}`);

    const enabledSwitch = page.getByRole("switch", { name: "SLA включён", exact: true });
    const resetSwitch = page.getByRole("switch", {
      name: "Сбрасывать отсчёт при ответе оператора",
      exact: true,
    });
    check((await enabledSwitch.count()) === 1, "Переключатель «SLA включён» найден");
    check((await resetSwitch.count()) === 1, "Переключатель «Сбрасывать отсчёт при ответе оператора» найден");

    const target = page.getByLabel("Цель ответа, секунд", { exact: true });
    check((await target.count()) === 1, "Числовой инпут «Цель ответа, секунд» найден");
    check(
      (await target.getAttribute("inputmode")) === "numeric",
      "Инпут секунд имеет inputMode=numeric",
      (await target.getAttribute("inputmode")) ?? "null",
    );
    const shownSeconds = (await target.inputValue()).trim();
    check(
      shownSeconds === String(baseline.sla_target_seconds),
      "Инпут показывает значение с сервера",
      `ui=${shownSeconds} api=${baseline.sla_target_seconds}`,
    );

    const humanized = await page.locator("text=/\\d+ (минут|час)/").first().innerText();
    check(humanized.length > 0, `Рядом показана человекочитаемая цель: «${humanized}»`);

    const save = page.getByRole("button", { name: "Сохранить", exact: true });
    check((await save.count()) === 1, "Кнопка «Сохранить» присутствует");
    check(await save.isDisabled(), "«Сохранить» погашена, пока изменений нет");

    const beforeChecked = (await enabledSwitch.getAttribute("aria-checked")) === "true";
    check(
      beforeChecked === baseline.sla_enabled,
      "Состояние «SLA включён» совпадает с API",
      `ui=${beforeChecked} api=${baseline.sla_enabled}`,
    );

    /* --- правка и сохранение --- */
    await enabledSwitch.click();
    await page.waitForTimeout(300);
    check(
      ((await enabledSwitch.getAttribute("aria-checked")) === "true") === !beforeChecked,
      "Переключатель изменил состояние по клику",
    );
    check(!(await save.isDisabled()), "«Сохранить» активна после изменения");

    const patchPromise = page.waitForResponse(
      (response) =>
        response.url().includes("/api/admin/settings") && response.request().method() === "PATCH",
      { timeout: 30_000 },
    );
    await save.click();
    const patchResponse = await patchPromise;
    check(patchResponse.status() === 200, "PATCH /api/admin/settings ответил 200", `${patchResponse.status()}`);

    await page.getByText("Настройки SLA сохранены", { exact: true }).waitFor({ state: "visible", timeout: 15_000 });
    check(true, "Показан тост «Настройки SLA сохранены»");

    const afterSave = await apiGetSettings(session.access_token);
    check(
      afterSave.sla_enabled === !baseline.sla_enabled,
      "Сервер сохранил новое значение sla_enabled",
      `api=${afterSave.sla_enabled}`,
    );
    check(await save.isDisabled(), "После сохранения «Сохранить» снова погашена");

    /* --- перезагрузка: значение должно прийти из БД --- */
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    await openSlaTab(page);
    const reloadedSwitch = page.getByRole("switch", { name: "SLA включён", exact: true });
    const reloadedChecked = (await reloadedSwitch.getAttribute("aria-checked")) === "true";
    check(
      reloadedChecked === !baseline.sla_enabled,
      "После перезагрузки вкладка показывает сохранённое значение",
      `ui=${reloadedChecked} ожидалось=${!baseline.sla_enabled}`,
    );

    const updatedBy = await page.locator("text=Кто сохранил").first().innerText().catch(() => "");
    check(updatedBy.includes("admin"), "Внизу подписано, кто сохранил", updatedBy);

    await page.screenshot({ path: SHOT, fullPage: false });
    check(fs.existsSync(SHOT), `Скриншот сохранён: ${SHOT}`);

    /* --- возврат исходного значения --- */
    await reloadedSwitch.click();
    await page.waitForTimeout(300);
    const revertPromise = page.waitForResponse(
      (response) =>
        response.url().includes("/api/admin/settings") && response.request().method() === "PATCH",
      { timeout: 30_000 },
    );
    await page.getByRole("button", { name: "Сохранить", exact: true }).click();
    const revertResponse = await revertPromise;
    check(revertResponse.status() === 200, "Откат: PATCH ответил 200", `${revertResponse.status()}`);

    const restored = await apiGetSettings(session.access_token);
    check(
      restored.sla_enabled === baseline.sla_enabled,
      "Исходное значение sla_enabled восстановлено",
      `api=${restored.sla_enabled} ожидалось=${baseline.sla_enabled}`,
    );

    /* --- SUPERVISOR: всё погашено --- */
    const supervisorData = await probeSupervisorAccess();
    console.log(
      `[info] supervisor GET -> ${supervisorData.getStatus}, PATCH -> ${supervisorData.patchStatus}`,
    );
    check(
      supervisorData.patchStatus === 403,
      "Запись настроек остаётся admin-only (403)",
      `${supervisorData.patchStatus}`,
    );

    const supervisorLoggedIn = await loginAdmin(page, SUPERVISOR_USER, SUPERVISOR_PASS);
    check(supervisorLoggedIn, "Супервизор вошёл в панель");
    if (supervisorLoggedIn) {
      await openSlaTab(page);

      const supSwitch = page.getByRole("switch", { name: "SLA включён", exact: true });
      const supTarget = page.getByLabel("Цель ответа, секунд", { exact: true });
      const supSave = page.getByRole("button", { name: "Сохранить", exact: true });

      if (supervisorData.getStatus === 200) {
        check(
          (await supSwitch.count()) === 1 && (await supTarget.count()) === 1,
          "Супервизору вкладка SLA доступна для просмотра",
        );
      } else {
        check(
          (await supSwitch.count()) === 0,
          "При 403 на чтение супервизор не видит рабочий переключатель",
          `GET=${supervisorData.getStatus}`,
        );
      }
      if ((await supSwitch.count()) === 1) {
        check(await supSwitch.isDisabled(), "Переключатель SLA погашен для супервизора");
        check(
          await page
            .getByRole("switch", { name: "Сбрасывать отсчёт при ответе оператора", exact: true })
            .isDisabled(),
          "Переключатель сброса отсчёта погашен для супервизора",
        );
      }
      if ((await supTarget.count()) === 1) {
        check(await supTarget.isDisabled(), "Числовой инпут погашен для супервизора");
      }
      if ((await supSave.count()) === 1) {
        check(await supSave.isDisabled(), "«Сохранить» погашена для супервизора");
      }

      await page.getByRole("button", { name: "Операторы", exact: true }).click();
      await page.waitForTimeout(1800);
      const createOperator = page.getByRole("button", { name: "Создать оператора", exact: true });
      check(await createOperator.isDisabled(), "«Создать оператора» погашена для супервизора");

      const editButtons = page.getByRole("button", { name: "Изменить", exact: true });
      const editCount = await editButtons.count();
      let allDisabled = editCount > 0;
      for (let index = 0; index < editCount; index += 1) {
        if (!(await editButtons.nth(index).isDisabled())) allDisabled = false;
      }
      check(allDisabled, "Все кнопки «Изменить» погашены для супервизора", `кнопок: ${editCount}`);

      await page.getByRole("button", { name: "Telegram-аккаунты", exact: true }).click();
      await page.waitForTimeout(1500);
      const addAccount = page.getByRole("button", { name: "Добавить аккаунт", exact: true });
      const hasAdd = (await addAccount.count()) === 1;
      check(!hasAdd || (await addAccount.isDisabled()), "«Добавить аккаунт» погашена для супервизора");
    }
  } catch (error) {
    check(false, "Скрипт завершился без исключения", String(error && error.message ? error.message : error));
    await page.screenshot({ path: "ui-shots/admin-sla-error.png" }).catch(() => undefined);
  } finally {
    await browser.close();

    /* Оборванный прогон не должен оставить стенд с перевёрнутым флагом. */
    try {
      const finalState = await apiGetSettings(session.access_token);
      if (finalState.sla_enabled !== baseline.sla_enabled) {
        await apiPatchSettings(session.access_token, { sla_enabled: baseline.sla_enabled });
        console.log(`[restore] sla_enabled -> ${baseline.sla_enabled}`);
      }
    } catch (restoreError) {
      console.log("[restore] не удалось вернуть исходное значение:", String(restoreError));
    }
  }

  const failed = results.filter((item) => !item.ok);
  console.log(`\nИТОГО: ${results.length - failed.length}/${results.length} проверок пройдено`);
  if (failed.length > 0) {
    console.log("Провалено:");
    for (const item of failed) console.log(` - ${item.label}${item.detail ? ` (${item.detail})` : ""}`);
    process.exit(1);
  }
  process.exit(0);
}

run().catch((error) => {
  console.error("Не удалось выполнить проверку:", error);
  process.exit(3);
});
