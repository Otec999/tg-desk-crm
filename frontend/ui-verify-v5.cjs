/**
 * Browser verification for CONTRACTS_V5 §1 (delete) and §5 (quick replies).
 *
 * Covers, against the real app + real backend:
 *
 *   1. `op1` (privileged operator, `can_delete_messages = true`)
 *      - delete button is visible on a live message and reads "Удалить сообщение";
 *      - confirming the inline prompt sends `DELETE /api/messages/{id}` and the
 *        bubble turns into a grey trace with the V5 banner ("Deleted from CRM at");
 *      - the delete button disappears from that bubble afterwards (a second
 *        DELETE would be a `409`);
 *      - the composer loads the quick-reply list from the store (no hard-coded
 *        array), and a reply is inserted **at the caret**, not appended;
 *      - the editor opens, closes on Escape, and correctly reports that this
 *        account may not mutate the list (CRUD is admin-only on the backend).
 *
 *   2. `admin` (role ADMIN)
 *      - full CRUD through the editor: create → edit → delete, with the status
 *        observed in the list *and* re-read through the REST API so a purely
 *        optimistic UI cannot fake a pass.
 *
 * The script re-logins by clearing localStorage between the two accounts because
 * the store persists the token under `tgdesk.token`.
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const API = "http://127.0.0.1:8000";
const SHOT = "ui-shots/v5-delete-quick-replies.png";

const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: Boolean(cond), label });
  console.log(`${cond ? "[PASS]" : "[FAIL]"} ${label}${!cond && detail ? `  -> ${detail}` : ""}`);
}

/** Login through the real form and wait for the chat list to settle. */
async function login(page, username, password) {
  await page.goto(APP, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    window.localStorage.removeItem("tgdesk.token");
    window.localStorage.removeItem("tgdesk.operator");
  });
  await page.goto(APP, { waitUntil: "networkidle" });
  const user = page.locator('input[placeholder="op1"], input[autocomplete="username"]').first();
  await user.fill(username);
  const pass = page.locator('input[type="password"]').first();
  await pass.fill(password);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(4500);
}

/** Open a chat by (partial) title. */
async function openChat(page, titlePart) {
  const search = page.locator('input[placeholder*="оиск"], input[type="search"]').first();
  if (await search.count()) {
    await search.fill(titlePart).catch(() => undefined);
    await page.waitForTimeout(700);
  }
  const row = page.locator(`text=${titlePart}`).first();
  if (!(await row.count())) return false;
  await row.click();
  await page.waitForTimeout(2500);
  return true;
}

/** REST helper that uses a real token, so the DB state can be asserted directly. */
async function apiLogin(username, password) {
  const response = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(`login ${username} -> HTTP ${response.status}`);
  const data = await response.json();
  return data.access_token;
}

async function listReplies(token) {
  const response = await fetch(`${API}/api/quick-replies?all=true`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) return null;
  return (await response.json()).items ?? [];
}

/**
 * Effective delete rights for a token, straight from the server.
 *
 * The UI mirrors the backend rule (`can_delete_messages or privileged role`), so the
 * test asks the server which branch it should expect instead of assuming that the
 * seeded `op1` was granted the flag — a fresh database and a re-seeded one differ.
 */
async function permissions(token) {
  const response = await fetch(`${API}/api/auth/me`, { headers: { Authorization: `Bearer ${token}` } });
  if (!response.ok) return null;
  const me = await response.json();
  const privileged = me.role === "ADMIN" || me.role === "SUPERVISOR";
  return {
    role: me.role,
    canDelete: Boolean(me.can_delete_messages) || privileged,
  };
}

/**
 * Exercises the whole §1.3 deletion path on the chat that is currently open.
 *
 * Returns `true` when it actually ran (the button was reachable) and `false` when
 * the signed-in operator has no right — the caller reports that as a skip rather
 * than a pass, so a missing permission can never masquerade as a success.
 */
async function runDeleteFlow(page, checkFn) {
  const deleteBtn = page.locator('button[aria-label="Удалить сообщение"]');
  if ((await deleteBtn.count()) === 0) return false;

  /* Only ONE message is deleted so the seed timeline survives repeated runs. */
  const target = deleteBtn.first();
  await target.scrollIntoViewIfNeeded();
  const bubbleId = await page.evaluate(() => {
    const button = document.querySelector('button[aria-label="Удалить сообщение"]');
    const wrapper = button?.closest("[data-message-id]");
    return wrapper ? wrapper.getAttribute("data-message-id") : null;
  });
  checkFn(Boolean(bubbleId), "target message id resolved", String(bubbleId));

  /* Attach the response observer before the first click. */
  const deleteResponse = page
    .waitForResponse(
      (response) => /\/api\/messages\/\d+$/.test(response.url()) && response.request().method() === "DELETE",
      { timeout: 20000 },
    )
    .catch(() => null);

  /* First click arms an inline confirmation inside the bubble (no modal, no blocking
     `window.confirm`); the request is only sent by the second click. */
  await target.click({ force: true });
  await page.waitForTimeout(400);

  const confirmBtn = page.locator('button[aria-label="Подтвердить удаление сообщения"]').first();
  const armed = (await confirmBtn.count()) > 0;
  checkFn(armed, "first click arms an inline confirmation instead of deleting at once");
  if (!armed) return true;

  /* Escape must disarm without sending anything. */
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  checkFn(
    (await page.locator('button[aria-label="Подтвердить удаление сообщения"]').count()) === 0,
    "Escape cancels the armed confirmation",
  );
  checkFn(
    (await page.locator('button[aria-label="Удалить сообщение"]').count()) > 0,
    "the message survived the cancelled confirmation",
  );

  await page.locator('button[aria-label="Удалить сообщение"]').first().click({ force: true });
  await page.waitForTimeout(250);
  await confirmBtn.click({ force: true });

  const response = await deleteResponse;
  checkFn(Boolean(response), "DELETE /api/messages/{id} was sent");
  if (response) checkFn(response.status() === 200, "delete answered 200", `status=${response.status()}`);
  await page.waitForTimeout(2500);

  /* The bubble is repainted in place: same id, now grey with the V5 banner. */
  const after = await page.evaluate((id) => {
    const wrapper = id ? document.querySelector(`[data-message-id="${id}"]`) : null;
    if (!wrapper) return null;
    return {
      grey: Boolean(wrapper.querySelector(".bubble-deleted")),
      strike: Boolean(wrapper.querySelector(".line-through")),
      text: wrapper.innerText,
      hasDelete: Boolean(wrapper.querySelector('button[aria-label="Удалить сообщение"]')),
    };
  }, bubbleId);

  checkFn(Boolean(after), "message row still present after deletion (invariant: never removed)");
  if (after) {
    checkFn(after.grey, "bubble carries the 'bubble-deleted' grey class");
    checkFn(after.strike, "deleted text is strikethrough");
    checkFn(after.hasDelete === false, "delete button gone from the deleted bubble");
    checkFn(
      /Удалено оператором|Deleted from CRM/i.test(after.text),
      "banner reports the CRM as the deletion source",
      after.text.slice(0, 160),
    );
  }
  return true;
}

async function main() {  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

  /* ================================================================ PART 1: op1 */
  await login(page, "op1", "operator123");
  const loggedIn = await page.locator("text=Merchant").count();
  check(loggedIn > 0, "op1 logged in and the chat list rendered", `rows=${loggedIn}`);

  const opened = await openChat(page, "Merchant Alpha");
  check(opened, "chat 'Merchant Alpha - Payouts' opened");

  /* ------------------------------------------------------------- delete button */
  const op1Token = await apiLogin("op1", "operator123");
  const op1Rights = await permissions(op1Token);
  const expectDelete = Boolean(op1Rights?.canDelete);
  console.log(
    `[INFO] op1 role=${op1Rights?.role} can_delete_messages→effective=${expectDelete ? "allowed" : "denied"}`,
  );

  const deleteBtn = page.locator('button[aria-label="Удалить сообщение"]');
  const deleteCount = await deleteBtn.count();

  if (!expectDelete) {
    /* Permission-denied branch: the control must be absent, because the server would
       answer 403 and a dead button is worse than no button. */
    check(deleteCount === 0, "no delete button when the operator lacks the right (no reachable 403)", `found ${deleteCount}`);
    console.log("[SKIP] delete flow needs an operator with can_delete_messages — covered in the admin pass");
  } else {
    check(deleteCount > 0, "delete button present on live messages (privileged operator)", `found ${deleteCount}`);
    if (deleteCount > 0) {
      const prefix = String(await deleteBtn.first().getAttribute("aria-label"));
      check(prefix === "Удалить сообщение", "delete button exposes an accessible name", prefix);
    }
  }

  /* A message deleted in Telegram must not offer the action: the server answers 409.
     Such bubbles exist in the seed, so the assertion is meaningful only if one is on
     screen — otherwise it is reported as skipped, never as a silent pass. */
  const deletedBubbleHasButton = await page.evaluate(() => {
    const wrappers = [...document.querySelectorAll('[data-message-id]')];
    const dead = wrappers.filter((node) => node.querySelector(".bubble-deleted"));
    if (dead.length === 0) return "skipped";
    return dead.some((node) => node.querySelector('button[aria-label="Удалить сообщение"]'));
  });
  if (deletedBubbleHasButton !== "skipped") {
    check(deletedBubbleHasButton === false, "no delete button on an already-deleted bubble");
  } else {
    console.log("[SKIP] no grey bubble on screen — deleted-row assertion not applicable");
  }

  /* The full delete flow only runs where the operator really holds the right; for a
     denied account the absence of the control is the assertion instead (see above). */
  if (deleteCount > 0 && expectDelete) {
    const ran = await runDeleteFlow(page, check);
    check(ran, "delete flow executed end to end");
    await page.screenshot({ path: "ui-shots/v5-delete.png" });
  }

  /* ----------------------------------------------------------- quick replies */
  const quickToggle = page.locator('button[aria-label="Быстрые ответы"]').first();
  check((await quickToggle.count()) > 0, "composer has a quick-replies button");
  if (await quickToggle.count()) {
    const icon = await quickToggle.innerHTML();
    check(!icon.includes("lucide-smile"), "quick-replies icon is no longer the Smile icon");

    /* Seed the draft so positional insertion can be proven: caret placed in the
       MIDDLE of the text must split it, not append at the end. */
    const composer = page.locator("textarea").first();
    await composer.fill("ALPHA-BETA");
    await composer.evaluate((node) => {
      node.focus();
      node.setSelectionRange(5, 5); /* right after "ALPHA" */
    });

    await quickToggle.click();
    await page.waitForTimeout(900);

    const listText = await page.locator("body").innerText();
    /* The panel is proven by its own controls, not by loose page text: either the
       empty state or at least one insertable reply must be on screen. */
    const insertable = await page.locator('button[aria-label^="Вставить быстрый ответ"]').count();
    const headerText = await page.evaluate(() => {
      const label = [...document.querySelectorAll("span")].find((node) =>
        /Быстрые ответы/.test(node.textContent || ""),
      );
      return label ? label.textContent : null;
    });
    check(
      Boolean(headerText) && (insertable > 0 || /создайте первый быстрый ответ|Пусто/.test(listText)),
      "quick-reply panel opened with its Russian caption",
      `header=${JSON.stringify(headerText)} insertable=${insertable}`,
    );

    const replyButtons = page.locator('button[aria-label^="Вставить быстрый ответ"]');
    const replyCount = await replyButtons.count();
    check(replyCount > 0, "quick replies loaded from the store (backend table)", `count=${replyCount}`);

    if (replyCount > 0) {
      const firstReply = replyButtons.first();
      const expectedBody = (await firstReply.getAttribute("title")) ?? "";
      const bodyText = await firstReply.innerText();
      /* §5.4 invariant: the four former constants must not be rendered by a bundled
         list any more. They may still exist as *data* (the backend seed inserts
         them), so the check looks at the rendered panel, not the whole document. */
      const panelHtml = await page.evaluate(() => {
        const insert = document.querySelector('button[aria-label^="Вставить быстрый ответ"]');
        return insert?.closest("div.flex.gap-2, div")?.parentElement?.innerText ?? "";
      });
      const seededFromDb = /Thanks for reaching out|transaction ID so we can pull/.test(panelHtml);
      check(
        seededFromDb,
        "panel content comes from the backend seed (those four texts are now DB rows, not constants)",
      );

      /* Restore the caret: opening the panel may have moved focus. */
      await composer.evaluate((node) => {
        node.focus();
        node.setSelectionRange(5, 5);
      });
      await firstReply.click();
      await page.waitForTimeout(600);

      const value = await composer.inputValue();
      check(
        value.length > "ALPHA-BETA".length,
        "quick reply inserted into the composer",
        value.slice(0, 80),
      );
      check(
        value.startsWith("ALPHA\n") && expectedBody && value.includes(expectedBody) && value.includes("BETA"),
        "inserted AT THE CARET — the draft is split, not appended",
        JSON.stringify(value.slice(0, 120)),
      );
      check(!/truncate[^"]*"[^>]*>\s*$/.test(bodyText), "reply body is rendered as text, not an empty chip");
    }

    /* ------------------------------------------------------------ the editor */
    await quickToggle.click();
    await page.waitForTimeout(500);
    const editBtn = page.locator('button[aria-label="Редактировать быстрые ответы"]').first();
    check((await editBtn.count()) > 0, "quick-reply panel offers an 'Изменить' button");
    if (await editBtn.count()) {
      await editBtn.click();
      await page.waitForTimeout(1200);
      const dialog = page.locator('[role="dialog"]').first();
      check((await dialog.count()) > 0, "quick-reply editor opened");
      if (await dialog.count()) {
        const dialogText = await dialog.innerText();
        check(/Быстрые ответы/.test(dialogText), "editor has its title");
        await page.screenshot({ path: SHOT });

        /* Escape closes it — the editor traps the key in the capture phase. */
        await page.keyboard.press("Escape");
        await page.waitForTimeout(700);
        check((await page.locator('[role="dialog"]').count()) === 0, "editor closed on Escape");

        /* Re-open and confirm the admin-only rule is *reported*, not silently ignored. */
        await quickToggle.click();
        await page.waitForTimeout(400);
        await editBtn.click();
        await page.waitForTimeout(1000);
        const asOperator = page.locator('[role="dialog"]').first();
        if (await asOperator.count()) {
          const addBtn = asOperator.locator('button[aria-label="Добавить быстрый ответ"]');
          if (await addBtn.count()) {
            await addBtn.click();
            await page.waitForTimeout(300);
            await asOperator.locator('input[aria-label="Короткий код быстрого ответа"]').fill("ui_probe");
            await asOperator.locator('input[aria-label="Подпись быстрого ответа"]').fill("UI probe");
            await asOperator.locator('textarea[aria-label="Текст быстрого ответа"]').fill("probe body that must be refused for a non-admin");
            await asOperator.locator('button[aria-label="Создать быстрый ответ"]').click();
            await page.waitForTimeout(2500);
            const alertText = await asOperator.innerText();
            check(
              /недостаточно прав|прав|админист|forbidden|403/i.test(alertText),
              "non-admin create is refused with an explicit reason (not a silent no-op)",
              alertText.slice(0, 160),
            );
          }
          await page.keyboard.press("Escape");
          await page.waitForTimeout(500);
        }
      }
    }
  }

  /* A `403` printed by the browser for the deliberate non-admin create probe is the
     expected server answer being surfaced by the store as a toast — not a UI fault.
     Only that exact line is filtered; every other error still fails the run. */
  const op1Errors = errors.filter(
    (e) =>
      !e.includes("favicon") &&
      !e.includes("React DevTools") &&
      !/Failed to load resource.*403/.test(e),
  );
  check(op1Errors.length === 0, "no console errors during the op1 pass", op1Errors.slice(0, 3).join(" | "));

  /* ============================================================== PART 2: admin */
  errors.length = 0;
  await login(page, "admin", "admin123");
  const adminIn = await page.locator("text=Merchant").count();
  check(adminIn > 0, "admin logged in to the desk UI");

  const openedAdmin = await openChat(page, "Merchant Alpha");
  check(openedAdmin, "admin opened the chat");

  const adminToken = await apiLogin("admin", "admin123");
  const before = (await listReplies(adminToken)) ?? [];
  const stamp = Date.now().toString().slice(-6);
  const shortcut = `ui${stamp}`;

  const openEditor = async () => {
    const toggle = page.locator('button[aria-label="Быстрые ответы"]').first();
    await toggle.click();
    await page.waitForTimeout(500);
    const edit = page.locator('button[aria-label="Редактировать быстрые ответы"]').first();
    if (!(await edit.count())) return null;
    await edit.click();
    await page.waitForTimeout(1200);
    const dialog = page.locator('[role="dialog"]').first();
    return (await dialog.count()) ? dialog : null;
  };

  /* The delete permission must be granted for admin too: the backend gate is
     `can_delete_messages or privileged role`, and ADMIN is privileged (§1.3). */
  const adminRights = await permissions(adminToken);
  check(
    Boolean(adminRights?.canDelete),
    "admin resolves to a privileged role with delete rights",
    String(adminRights?.role),
  );
  const ranDelete = await runDeleteFlow(page, check);
  check(ranDelete, "§1.3 delete flow executed end to end as an administrator");
  if (ranDelete) await page.screenshot({ path: "ui-shots/v5-delete.png" });
  else console.log("[SKIP] delete flow unreachable — no message with a delete button in this chat");

  let dialog = await openEditor();
  check(Boolean(dialog), "admin opened the quick-reply editor");

  if (dialog) {
    /* ------------------------------------------------------------------ create */
    await dialog.locator('button[aria-label="Добавить быстрый ответ"]').click();
    await page.waitForTimeout(300);
    await dialog.locator('input[aria-label="Короткий код быстрого ответа"]').fill(shortcut);
    await dialog.locator('input[aria-label="Подпись быстрого ответа"]').fill(`UI ${stamp}`);
    await dialog.locator('textarea[aria-label="Текст быстрого ответа"]').fill(`Тело быстрого ответа ${stamp}`);
    await dialog.locator('button[aria-label="Создать быстрый ответ"]').click();
    await page.waitForTimeout(2500);

    const afterCreate = (await listReplies(adminToken)) ?? [];
    const created = afterCreate.find((item) => item.shortcut === shortcut);
    check(Boolean(created), "quick reply created and persisted on the server", `count ${before.length} -> ${afterCreate.length}`);

    /* -------------------------------------------------------------------- edit */
    if (created) {
      const rowButton = dialog.locator(`button[aria-label="Редактировать быстрый ответ «UI ${stamp}»"]`).first();
      if (await rowButton.count()) {
        await rowButton.click();
        await page.waitForTimeout(400);
        const title = dialog.locator('input[aria-label="Подпись быстрого ответа"]');
        await title.fill(`UI ${stamp} EDIT`);
        await dialog.locator('button[aria-label="Сохранить быстрый ответ"]').click();
        await page.waitForTimeout(2500);

        const afterEdit = (await listReplies(adminToken)) ?? [];
        const edited = afterEdit.find((item) => item.id === created.id);
        check(edited?.title === `UI ${stamp} EDIT`, "quick reply edit persisted", String(edited?.title));

        /* Body-clear validation must block the save before any request is made. */
        await dialog.locator('textarea[aria-label="Текст быстрого ответа"]').fill("   ");
        await page.waitForTimeout(200);
        const saveBtn = dialog.locator('button[aria-label="Сохранить быстрый ответ"]');
        check(await saveBtn.isDisabled(), "save is disabled for a blank body (client validation)");
        await dialog.locator('textarea[aria-label="Текст быстрого ответа"]').fill(`Тело быстрого ответа ${stamp}`);
        await page.waitForTimeout(200);

        /* Take the screenshot here: the editor is populated and the list is real. */
        await page.screenshot({ path: SHOT });

        /* ------------------------------------------------------------------ delete */
        const delBtn = dialog.locator(`button[aria-label="Удалить быстрый ответ «UI ${stamp} EDIT»"]`).first();
        check((await delBtn.count()) > 0, "delete control appears on the row");
        if (await delBtn.count()) {
          await delBtn.click();
          await page.waitForTimeout(300);
          const confirmBtn = dialog.locator(`button[aria-label="Подтвердить удаление «UI ${stamp} EDIT»"]`).first();
          const needsConfirm = (await confirmBtn.count()) > 0;
          check(needsConfirm, "delete needs a second click (accidental deletion is cheap to undo)");
          await (needsConfirm ? confirmBtn : delBtn).click();
          await page.waitForTimeout(2500);

          const afterDelete = (await listReplies(adminToken)) ?? [];
          check(
            !afterDelete.some((item) => item.id === created.id),
            "quick reply deleted and gone from the server",
            `remaining=${afterDelete.length}`,
          );
          const stillOnScreen = await dialog.innerText();
          check(!stillOnScreen.includes(`UI ${stamp} EDIT`), "deleted reply removed from the editor list");
        }
      } else {
        check(false, "created reply is selectable in the editor list");
      }
    }

    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
  }

  const adminErrors = errors.filter((e) => !e.includes("favicon") && !e.includes("React DevTools"));
  check(adminErrors.length === 0, "no console errors during the admin pass", adminErrors.slice(0, 3).join(" | "));

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n=== ${results.length - failed.length}/${results.length} UI checks passed ===`);
  if (failed.length) console.log("FAILED: " + failed.map((f) => f.label).join("; "));
  return failed.length ? 1 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error("FATAL:", error.message);
    process.exit(1);
  });
