import { useEffect, useRef, useState, type ReactNode } from "react";

import { ChatList } from "./components/ChatList";
import { ChatWindow } from "./components/ChatWindow";
import { LoginScreen } from "./components/LoginScreen";
import { MobileDrawer, MobileToolbar, type DrawerKind } from "./components/MobileDrawer";
import { SearchOverlay } from "./components/SearchOverlay";
import { ToastViewport } from "./components/ToastViewport";
import { TopBar } from "./components/TopBar";
import { TxSidebar } from "./components/TxSidebar";
import AdminLoginPage from "./pages/AdminLoginPage";
import AdminPage from "./pages/AdminPage";
import { startPolling, stopPolling, useCRMStore } from "./store/useCRMStore";

/** Tailwind's `md` breakpoint: below it the chat list is a drawer, not a column. */
const MOBILE_QUERY = "(max-width: 767px)";

function isMobileViewport(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(MOBILE_QUERY).matches
    : false;
}

/* ------------------------------------------------------------------ routing
 *
 * CONTRACTS_V2 §6 — no router library, one switch over `window.location.pathname`:
 *
 *   /admin          -> the separate admin login form  (§3.3 `/api/admin/login`)
 *   /admin/panel    -> the admin console (and any nested path under it)
 *   everything else -> the operator CRM
 *
 * Trailing slashes are normalised away (`/admin/` === `/admin`) and matching is
 * exact for `/admin`, so `/administrator` can never be mistaken for the console.
 */

const ADMIN_LOGIN_PATH = "/admin";
const ADMIN_PANEL_PATH = "/admin/panel";

type Route = "crm" | "admin-login" | "admin-panel";

/** Normalise `pathname` (strip the trailing slash, keep the root as `/`). */
function normalizePath(pathname: string): string {
  if (!pathname) return "/";
  const trimmed = pathname.replace(/\/+$/, "");
  return trimmed === "" ? "/" : trimmed;
}

function resolveRoute(pathname: string): Route {
  const path = normalizePath(pathname);
  if (path === ADMIN_PANEL_PATH || path.startsWith(`${ADMIN_PANEL_PATH}/`)) return "admin-panel";
  if (path === ADMIN_LOGIN_PATH) return "admin-login";
  return "crm";
}

/** Keep the in-app route in sync when the operator presses Back/Forward. */
function usePathname(): string {
  const [pathname, setPathname] = useState<string>(() =>
    typeof window === "undefined" ? "/" : normalizePath(window.location.pathname),
  );

  useEffect(() => {
    const handlePopState = (): void => setPathname(normalizePath(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  return pathname;
}

export default function App(): JSX.Element {
  const pathname = usePathname();
  const route = resolveRoute(pathname);

  /* The admin console is a completely separate session: it must not boot the
     operator store, open the CRM WebSocket or start the SLA ticker. */
  if (route === "admin-login") {
    return (
      <Shell>
        <AdminLoginPage />
        <ToastViewport />
      </Shell>
    );
  }

  if (route === "admin-panel") {
    return (
      <Shell>
        <AdminPage />
        <ToastViewport />
      </Shell>
    );
  }

  return <CrmApp />;
}

function CrmApp(): JSX.Element {
  const operator = useCRMStore((state) => state.operator);
  const bootstrap = useCRMStore((state) => state.bootstrap);
  const tick = useCRMStore((state) => state.tick);
  const mode = useCRMStore((state) => state.mode);
  const searchOpen = useCRMStore((state) => state.searchOpen);
  const openSearch = useCRMStore((state) => state.openSearch);

  const [drawer, setDrawer] = useState<DrawerKind>(null);

  useEffect(() => {
    void bootstrap();
    const timer = window.setInterval(tick, 1000);
    return () => {
      window.clearInterval(timer);
      stopPolling();
    };
  }, [bootstrap, tick]);

  /**
   * CONTRACTS_V5 §6.3 — global search hotkey: `Ctrl+K` (Windows/Linux) and
   * `⌘K` (macOS).
   *
   * `event.key` is compared case-insensitively because `Shift+K` reports `"K"`,
   * and the `keydown` is captured so a focused textarea cannot swallow it. The
   * handler keeps its own `preventDefault`: Chrome opens the address-bar search
   * on `Ctrl+K`. Guarded on `operator` so the shortcut stays inert on the login
   * screen.
   */
  useEffect(() => {
    if (!operator) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      if (event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      event.stopPropagation();
      openSearch();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [operator, openSearch]);

  /**
   * On phones the chat list lives in a drawer (`md:` and up shows it as a column), so a
   * freshly logged-in operator would otherwise land on an empty "select a chat" pane with
   * no hint that the groups exist. Open the list once per browser session.
   */
  const openedListRef = useRef(false);
  useEffect(() => {
    if (!operator || openedListRef.current) return;
    if (useCRMStore.getState().selectedChatId === null && isMobileViewport()) {
      openedListRef.current = true;
      setDrawer("chats");
    }
  }, [operator]);

  useEffect(() => {
    if (operator && mode === "live") startPolling();
    return () => stopPolling();
  }, [operator, mode]);

  if (!operator) {
    return (
      <Shell>
        <LoginScreen />
        <ToastViewport />
      </Shell>
    );
  }

  return (
    <Shell>
      <TopBar />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* left: chat list */}
        <div className="hidden w-[340px] shrink-0 border-r border-tg-border md:flex md:flex-col xl:w-[372px]">
          <ChatList />
        </div>

        {/* center: telegram chat */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-1 border-b border-tg-border bg-tg-panel px-2 py-1 md:hidden">
            <MobileToolbar onOpen={setDrawer} />
            <button
              type="button"
              onClick={() => setDrawer("lookup")}
              className="ml-auto rounded-md border border-tg-border bg-tg-elevated px-2 py-1 text-2xs font-semibold text-tg-accentHover"
            >
              TX Lookup
            </button>
          </div>
          <ChatWindow />
        </div>

        {/* right: dual admin lookup */}
        <div className="hidden w-[340px] shrink-0 flex-col border-l border-tg-border xl:flex">
          <TxSidebar />
        </div>
      </div>

      <MobileDrawer kind={drawer} onClose={() => setDrawer(null)} />
      {/* CONTRACTS_V5 §6.3 — a portal on `document.body`; routing is untouched. */}
      {searchOpen && <SearchOverlay />}
      <ToastViewport />
    </Shell>
  );
}

function Shell({ children }: { children: ReactNode }): JSX.Element {
  return <div className="flex h-screen w-screen flex-col overflow-hidden bg-tg-bg text-tg-text">{children}</div>;
}
