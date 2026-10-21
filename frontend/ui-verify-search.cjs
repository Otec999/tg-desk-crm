/**
 * Browser check for the global search overlay (CONTRACTS_V5 §6).
 *
 * Flow
 * 1. Log in as `op1` and land on the CRM.
 * 2. Press Ctrl+K → the overlay must open and the input must be autofocused.
 * 3. Type a substring that really exists in the database and assert that grouped
 *    hits render **and** that `<mark>` highlighting is present.
 * 4. Click a hit → the chat opens and the message flashes (`.flash-target` /
 *    `#msg-<id>`), i.e. the `selectChat` context-preservation actually works.
 * 5. Escape closes the overlay; no console errors are allowed.
 *
 * The query is discovered from the live API (`GET /api/search`) with a fallback
 * to known database rows, so the script stays valid as the data changes. When
 * the backend endpoint is not deployed yet the run reports the graceful-error
 * path instead of failing silently.
 */

const { chromium } = require("playwright");

const APP = "http://127.0.0.1:5173";
const API = "http://localhost:8000";

/** Phrases that are known to exist in the seeded `group_messages` rows. */
const FALLBACK_QUERIES = ["payout", "вопрос", "Telegram", "help"];

const results = [];

function check(cond, label, detail = "") {
  results.push({ ok: Boolean(cond), label });
  console.log(`${cond ? "[PASS]" : "[FAIL]"} ${label}${!cond && detail ? `  -> ${detail}` : ""}`);
  return Boolean(cond);
}

function info(label, detail = "") {
  console.log(`[INFO] ${label}${detail ? `  -> ${detail}` : ""}`);
}

/** Obtain a token so the script can inspect `/api/search` directly. */
async function apiToken() {
  try {
    const response = await fetch(`${API}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "op1", password: "operator123" }),
    });
    if (!response.ok) return null;
    const body = await response.json();
    return body.access_token || null;
  } catch {
    return null;
  }
}

/** Probe `/api/search`; returns `{ ok, status, items }` and never throws. */
async function probeSearch(token, query) {
  if (!token) return { ok: false, status: 0, items: [] };
  try {
    const response = await fetch(`${API}/api/search?q=${encodeURIComponent(query)}&limit=20`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return { ok: false, status: response.status, items: [] };
    const body = await response.json();
    return { ok: true, status: response.status, items: Array.isArray(body.items) ? body.items : [] };
  } catch {
    return { ok: false, status: 0, items: [] };
  }
}

/**
 * Pick a query that is guaranteed to hit: the backend is authoritative when it
 * is deployed, otherwise the seeded fallbacks are tried in order.
 */
async function resolveQuery(token) {
  const probe = await probeSearch(token, FALLBACK_QUERIES[0]);
  if (probe.ok) {
    for (const candidate of FALLBACK_QUERIES) {
      const result = await probeSearch(token, candidate);
      if (result.items.length > 0) {
        info("query resolved from /api/search", `${JSON.stringify(candidate)} → ${result.items.length} hits`);
        return { query: candidate, backendReady: true, hits: result.items };
      }
    }
    info("backend search is live but returned no hits for the fallbacks");
    return { query: FALLBACK_QUERIES[0], backendReady: true, hits: [] };
  }
  info("backend /api/search is NOT ready", `HTTP ${probe.status}`);
  return { query: FALLBACK_QUERIES[0], backendReady: false, hits: [] };
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(`pageerror: ${error.message}`));

  /* ---------------------------------------------------------- 1. login */
  await page.goto(APP, { waitUntil: "networkidle" });
  await page.locator('input[placeholder="op1"]').first().fill("op1");
  await page.locator('input[type="password"]').first().fill("operator123");
  await page.keyboard.press("Enter");

  /* Wait for the CRM to actually render instead of sleeping a fixed 4.5 s: a
     slow (or just-restarted) backend used to make this run flaky. */
  const loggedIn = await page
    .locator('[data-testid="open-global-search"]')
    .first()
    .waitFor({ state: "visible", timeout: 30000 })
    .then(() => true)
    .catch(() => false);
  check(loggedIn, "CRM loaded (search button visible)");
  if (!loggedIn) {
    await page.screenshot({ path: "ui-shots/v5-search-login-failed.png" });
    await browser.close();
    return 1;
  }

  const token = await apiToken();
  const { query, backendReady } = await resolveQuery(token);
  info("search query under test", query);

  /* ------------------------------------------------ 2. Ctrl+K opens it */
  await page.keyboard.press("Control+k");

  const overlay = page.locator('[data-testid="search-overlay"]');
  const opened = await overlay
    .waitFor({ state: "visible", timeout: 10000 })
    .then(() => true)
    .catch(() => false);
  check(opened, "Ctrl+K opens the search overlay");
  if (!opened) {
    await page.screenshot({ path: "ui-shots/v5-search-overlay-missing.png" });
    await browser.close();
    return 1;
  }

  const input = page.locator('[data-testid="search-input"]');
  check((await input.count()) > 0, "search input is rendered");
  check(await input.first().evaluate((el) => el === document.activeElement), "input is autofocused");

  const placeholder = await input.first().getAttribute("placeholder");
  check(placeholder === "Поиск по всем сообщениям", "placeholder matches the contract", String(placeholder));

  /* ---------------------------------------------------- 3. type & wait */
  const hitLocator = page.locator('[data-testid="search-overlay"] [data-testid="search-hit"]');
  const errorBox = page.locator('[data-testid="search-error"]');
  await input.first().fill(query);

  /* Deterministic wait: the overlay settles either on hits, on an error box or
     on the explicit "nothing found" panel — whichever comes first. */
  if (backendReady) {
    await hitLocator
      .first()
      .waitFor({ state: "visible", timeout: 20000 })
      .catch(() => undefined);
  } else {
    await errorBox
      .first()
      .waitFor({ state: "visible", timeout: 20000 })
      .catch(() => undefined);
  }

  const hitCount = await hitLocator.count();
  const hasError = (await errorBox.count()) > 0;

  if (backendReady) {
    check(hitCount > 0, `results rendered for ${JSON.stringify(query)}`, `${hitCount} hits`);
    if (hitCount > 0) {
      const marks = await page.locator('[data-testid="search-hit-mark"]').count();
      check(marks > 0, "match highlighting (<mark>) is present", `${marks} marks`);
      const groupHeaders = await page.locator('[data-testid="search-overlay"] section header').count();
      check(groupHeaders > 0, "hits are grouped by chat", `${groupHeaders} groups`);
      await page.screenshot({ path: "ui-shots/v5-search-results.png" });

      /* --------------------------------------- 4. click a hit → jump */
      const firstHit = hitLocator.first();
      const messageId = await firstHit.getAttribute("data-message-id");
      const chatId = await firstHit.getAttribute("data-chat-id");
      info("opening hit", `chat=${chatId} message=${messageId}`);

      await firstHit.click();

      const node = page.locator(`#msg-${messageId}`);
      /* The timeline mounts after the context window is stitched in; give the
         store a real chance instead of a fixed sleep. Keep the window short: the
         highlight is cleared ~1.9 s after `jumpToMessage`. */
      const nodeVisible = await node
        .waitFor({ state: "attached", timeout: 4000 })
        .then(() => true)
        .catch(() => false);

      check((await page.locator('[data-testid="search-overlay"]').count()) === 0, "overlay closes after a click");
      check(nodeVisible, `target message #msg-${messageId} is in the timeline`);
      if (nodeVisible) {
        const inViewport = await node.evaluate((el) => {
          const rect = el.getBoundingClientRect();
          return rect.top < window.innerHeight && rect.bottom > 0;
        });
        check(inViewport, "target message was scrolled into the viewport");
        const classes = (await node.getAttribute("class")) || "";
        check(classes.includes("flash-target"), "target message carries the flash highlight", classes.slice(0, 160));
      }
      await page.screenshot({ path: "ui-shots/v5-search-jumped.png" });
      await page.waitForTimeout(1500);

      /* ------------------------------------------- 5. Escape reopens/closes */
      await page.keyboard.press("Control+k");
      await page
        .locator('[data-testid="search-overlay"]')
        .waitFor({ state: "visible", timeout: 10000 })
        .catch(() => undefined);
      check((await page.locator('[data-testid="search-overlay"]').count()) > 0, "Ctrl+K reopens the overlay");
      await page.keyboard.press("Escape");
      await page
        .locator('[data-testid="search-overlay"]')
        .waitFor({ state: "detached", timeout: 10000 })
        .catch(() => undefined);
      check((await page.locator('[data-testid="search-overlay"]').count()) === 0, "Escape closes the overlay");
    }
  } else {
    /* The backend agent has not shipped /api/search yet — the UI must degrade. */
    info("end-to-end search needs the backend endpoint", "/api/search is 404");
    check(hitCount === 0, "no phantom results without a backend", `${hitCount} hits`);
    check(hasError, "a visible error state is shown instead of a blank panel");
    if (hasError) {
      const text = (await errorBox.first().innerText()).replace(/\s+/g, " ").trim();
      info("error message shown", text);
    }
    await page.screenshot({ path: "ui-shots/v5-search-error.png" });

    await page.keyboard.press("Escape");
    await page
      .locator('[data-testid="search-overlay"]')
      .waitFor({ state: "detached", timeout: 10000 })
      .catch(() => undefined);
    check((await page.locator('[data-testid="search-overlay"]').count()) === 0, "Escape closes the overlay");
  }

  /* ----------------------------------------------- keyboard navigation */
  if (backendReady && hitCount > 1) {
    await page.keyboard.press("Control+k");
    await page
      .locator('[data-testid="search-input"]')
      .waitFor({ state: "visible", timeout: 10000 })
      .catch(() => undefined);
    await page.locator('[data-testid="search-input"]').fill(query);
    await hitLocator.first().waitFor({ state: "visible", timeout: 20000 }).catch(() => undefined);

    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    /* Wait for the selection to land instead of a fixed pause. */
    await page
      .locator('[data-testid="search-overlay"] [role="option"][aria-selected="true"]')
      .first()
      .waitFor({ state: "visible", timeout: 5000 })
      .catch(() => undefined);
    /* Scoped to the overlay: `ChatList` rows behind it also use role="option". */
    const selected = await page
      .locator('[data-testid="search-overlay"] [role="option"][aria-selected="true"]')
      .count();
    check(selected === 1, "ArrowDown moves a single selection", `${selected} selected`);
    await page.keyboard.press("Escape");
    await page
      .locator('[data-testid="search-overlay"]')
      .waitFor({ state: "detached", timeout: 10000 })
      .catch(() => undefined);
  }

  /* ------------------------------------------------------- console log */
  const realErrors = consoleErrors.filter(
    (text) =>
      !text.includes("favicon") &&
      !text.includes("React DevTools") &&
      /* Expected while the backend endpoint is missing: the fetch 404 itself. */
      !(text.includes("/api/search") && text.includes("404")),
  );
  check(realErrors.length === 0, "no console errors", realErrors.slice(0, 3).join(" | "));

  await browser.close();

  const failed = results.filter((entry) => !entry.ok);
  console.log(`\n=== ${results.length - failed.length}/${results.length} search UI checks passed ===`);
  if (!backendReady) {
    console.log("NOTE: end-to-end search requires the backend GET /api/search (CONTRACTS_V5 §6.2).");
  }
  if (failed.length) console.log("FAILED: " + failed.map((entry) => entry.label).join("; "));
  return failed.length ? 1 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error("FATAL:", error.message);
    process.exit(1);
  });
