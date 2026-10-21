import { AlertTriangle, CornerDownLeft, Loader2, Search, SearchX, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { avatarColor, formatChatListTime, formatClock, initials } from "../lib/format";
import type { SearchHit, SearchSenderFilter } from "../lib/types";
import { SEARCH_SENDER_FILTERS } from "../lib/types";
import { selectSearchResults, useCRMStore } from "../store/useCRMStore";

/** Sender tabs of CONTRACTS_V5 §6.2 (`sender=all|client|operator`). */
const SENDER_LABELS: Record<SearchSenderFilter, string> = {
  all: "Все",
  client: "Клиент",
  operator: "Оператор",
};

/** A hit is identified by chat + message: one message can only live in one chat. */
interface HitKey {
  chatId: number;
  messageId: number;
}

function keyOf(hit: SearchHit): HitKey {
  return { chatId: hit.chat_id, messageId: hit.message.id };
}

function sameKey(left: HitKey | null, right: HitKey): boolean {
  return left !== null && left.chatId === right.chatId && left.messageId === right.messageId;
}

interface SenderGroup {
  chatId: number;
  title: string;
  merchantCode: string | null;
  hits: SearchHit[];
}

/**
 * Group the flat, score-ranked hit list by chat while keeping the backend order.
 *
 * The first hit of a chat is by definition its best one (the list is already
 * sorted by `score` DESC, then `created_at` DESC), so both the group order and
 * the order inside a group stay meaningful.
 */
function groupByChat(hits: readonly SearchHit[]): SenderGroup[] {
  const groups = new Map<number, SenderGroup>();
  for (const hit of hits) {
    const existing = groups.get(hit.chat_id);
    if (existing) {
      existing.hits.push(hit);
      continue;
    }
    groups.set(hit.chat_id, {
      chatId: hit.chat_id,
      title: hit.chat_title,
      merchantCode: hit.chat_merchant_code,
      hits: [hit],
    });
  }
  return [...groups.values()];
}

/** `agent` (not `operator`) drives the accent colour: matches Telegram's outgoing tint. */
function senderLabel(hit: SearchHit): string {
  const name = hit.message.sender_name?.trim();
  if (name) return name;
  return hit.message.is_outgoing ? "Оператор" : "Клиент";
}

/**
 * Render `snippet` with every `match_spans` range wrapped in `<mark>`.
 *
 * The spans come from the backend and are `[start, end)` offsets into `snippet`
 * itself. They are clamped, sorted and de-overlapped defensively: a malformed or
 * overlapping range must never produce a broken tree or drop text between two
 * highlights (the characters in between are emitted as plain segments).
 *
 * Falls back to a case-insensitive substring search when the backend sent no
 * spans (older builds, or a title-only match) so a hit is never visually mute.
 */
export function highlightSnippet(snippet: string, spans: readonly [number, number][], needle: string): ReactNode[] {
  const ranges: [number, number][] = [];
  const length = snippet.length;

  for (const raw of spans) {
    const start = Math.max(0, Math.min(length, raw[0]));
    const end = Math.max(start, Math.min(length, raw[1]));
    if (end > start) ranges.push([start, end]);
  }

  if (ranges.length === 0) {
    const query = needle.trim().toLowerCase();
    if (query) {
      const haystack = snippet.toLowerCase();
      let cursor = haystack.indexOf(query);
      while (cursor !== -1) {
        ranges.push([cursor, cursor + query.length]);
        cursor = haystack.indexOf(query, cursor + query.length);
      }
    }
  }

  if (ranges.length === 0) return [snippet];

  ranges.sort((left, right) => left[0] - right[0] || left[1] - right[1]);

  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    /* Skip a range that overlaps one already emitted (keeps the text contiguous). */
    if (start < cursor) continue;
    if (start > cursor) nodes.push(snippet.slice(cursor, start));
    nodes.push(
      <mark
        key={`${start}-${end}`}
        className="rounded-sm bg-tg-accent/35 px-0.5 text-tg-text"
        data-testid="search-hit-mark"
      >
        {snippet.slice(start, end)}
      </mark>,
    );
    cursor = end;
  }
  if (cursor < snippet.length) nodes.push(snippet.slice(cursor));
  return nodes;
}

/**
 * Global Telegram-style search overlay — CONTRACTS_V5 §6.3.
 *
 * Rendering is delegated to the portal on `document.body`, so the overlay sits
 * above the three-column CRM layout without touching the (router-less) routing
 * in `App.tsx`. All search state lives in `useCRMStore`; the component itself
 * only owns the keyboard cursor.
 *
 * Keyboard model:
 * - `Escape` closes the overlay (capture phase — beats the composer's handler);
 * - `ArrowDown` / `ArrowUp` move the selection through the flat hit list;
 * - `Enter` opens the selected hit (or the first one when nothing is selected).
 *
 * Listener hygiene mirrors `ForwardDialog`: one effect, one cleanup, callbacks
 * read through refs so a new inline arrow from the parent never re-subscribes.
 */
export function SearchOverlay(): JSX.Element | null {
  const open = useCRMStore((state) => state.searchOpen);
  const query = useCRMStore((state) => state.searchQuery);
  const loading = useCRMStore((state) => state.searchLoading);
  const error = useCRMStore((state) => state.searchError);
  const sender = useCRMStore((state) => state.searchSender);
  const results = useCRMStore(selectSearchResults);
  const setSearchQuery = useCRMStore((state) => state.setSearchQuery);
  const setSearchSender = useCRMStore((state) => state.setSearchSender);
  const clearSearch = useCRMStore((state) => state.clearSearch);
  const closeSearch = useCRMStore((state) => state.closeSearch);
  const runSearch = useCRMStore((state) => state.runSearch);
  const openMessageInChat = useCRMStore((state) => state.openMessageInChat);

  const [cursor, setCursor] = useState<HitKey | null>(null);
  const [opening, setOpening] = useState(false);

  const titleId = useId();
  const listId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  /* Latest callbacks behind refs — the global listener subscribes once. */
  const closeRef = useRef(closeSearch);
  closeRef.current = closeSearch;
  const runSearchRef = useRef(runSearch);
  runSearchRef.current = runSearch;

  /* The keyboard cursor is mirrored into a ref: the global listener runs outside
     React's render cycle and must never close over a stale `cursor`. */
  const cursorRef = useRef<HitKey | null>(cursor);
  cursorRef.current = cursor;

  const groups = useMemo(() => groupByChat(results), [results]);
  const flat = results;

  /* `cursor` must survive a new result page only if that hit is still listed. */
  useEffect(() => {
    if (cursor !== null && !flat.some((hit) => sameKey(cursor, keyOf(hit)))) setCursor(null);
  }, [flat, cursor]);

  const selectableIndex = useMemo(() => {
    if (cursor === null) return -1;
    return flat.findIndex((hit) => sameKey(cursor, keyOf(hit)));
  }, [flat, cursor]);

  /* -------------------------------------------- mount / focus / scroll lock */
  useEffect(() => {
    if (!open) return;

    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    /* Autofocus once the portal is attached. */
    const focusTimer = window.setTimeout(() => inputRef.current?.focus(), 0);

    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      const restoreTo = restoreFocusRef.current;
      if (restoreTo && document.contains(restoreTo)) restoreTo.focus({ preventScroll: true });
      restoreFocusRef.current = null;
    };
  }, [open]);

  /* --------------------------------------------------------- open one hit */
  const openHit = useCallback(async (hit: SearchHit): Promise<void> => {
    setOpening(true);
    try {
      await openMessageInChat(hit.chat_id, hit.message.id);
    } finally {
      setOpening(false);
      setCursor(null);
    }
  }, [openMessageInChat]);

  /* `openHit` is recreated when the store action identity changes; the global
     listener must still see the newest one without re-subscribing. */
  const openHitLatest = useRef(openHit);
  openHitLatest.current = openHit;

  /* ------------------------------------------------------------- keyboard */
  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
        return;
      }

      const hits = useCRMStore.getState().searchResults;
      if (hits.length === 0) {
        /* Nothing to navigate — Enter re-runs the query (e.g. after an error). */
        if (event.key === "Enter" && !event.shiftKey && useCRMStore.getState().searchQuery.trim()) {
          event.preventDefault();
          void runSearchRef.current();
        }
        return;
      }

      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const currentIndex =
          cursorRef.current === null ? -1 : hits.findIndex((hit) => sameKey(cursorRef.current, keyOf(hit)));
        const nextIndex =
          currentIndex === -1 ? (step === 1 ? 0 : hits.length - 1) : (currentIndex + step + hits.length) % hits.length;
        const next = hits[nextIndex];
        if (!next) return;
        cursorRef.current = keyOf(next);
        setCursor(keyOf(next));
        /* Keep the selected row inside the scroll viewport. */
        document
          .getElementById(`search-hit-${next.chat_id}-${next.message.id}`)
          ?.scrollIntoView({ block: "nearest" });
        return;
      }

      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const target =
          cursorRef.current === null
            ? hits[0]
            : (hits.find((hit) => sameKey(cursorRef.current, keyOf(hit))) ?? hits[0]);
        if (!target) return;
        void openHitLatest.current(target);
      }
    };

    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [open]);

  /* ------------------------------------------------------------- handlers */
  const handleBackdropMouseDown = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    event.preventDefault();
    closeRef.current();
  }, []);

  const handlePanelMouseDown = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    event.stopPropagation();
  }, []);

  const handleQueryChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setCursor(null);
      setSearchQuery(event.target.value);
    },
    [setSearchQuery],
  );

  const handleClear = useCallback(() => {
    setCursor(null);
    clearSearch();
    inputRef.current?.focus();
  }, [clearSearch]);

  const handleKeyUp = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      /* Enter in the box either opens the selected hit (handled globally) or,
         with nothing selected and nothing found yet, runs the query now. */
      if (event.key !== "Enter") return;
      if (results.length === 0 && !loading && query.trim()) void runSearch();
    },
    [results.length, loading, query, runSearch],
  );

  if (!open) return null;
  if (typeof document === "undefined") return null;

  const trimmed = query.trim();
  const showEmpty = !loading && error === null && trimmed.length > 0 && results.length === 0;
  const showIdle = !loading && error === null && trimmed.length === 0;
  const selectedHit = selectableIndex === -1 ? null : flat[selectableIndex];

  return createPortal(
    <div
      className="fixed inset-0 z-[130] flex animate-fade-in items-start justify-center px-4 pb-4 pt-[8vh] sm:pt-[10vh]"
      style={{ backgroundColor: "rgba(0, 0, 0, 0.72)" }}
      onMouseDown={handleBackdropMouseDown}
      data-testid="search-overlay"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex max-h-[80vh] w-full max-w-2xl animate-slide-up flex-col overflow-hidden rounded-xl border border-tg-border bg-tg-panel shadow-pop"
        onMouseDown={handlePanelMouseDown}
      >
        <h2 id={titleId} className="sr-only">
          Поиск по всем сообщениям
        </h2>

        {/* ----------------------------------------------------------- input */}
        <div className="flex shrink-0 items-center gap-2 border-b border-tg-border px-3 py-2.5">
          {loading ? (
            <Loader2 size={16} className="shrink-0 animate-spin text-tg-accentHover" aria-hidden />
          ) : (
            <Search size={16} className="shrink-0 text-tg-textFaint" aria-hidden />
          )}
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={handleQueryChange}
            onKeyUp={handleKeyUp}
            placeholder="Поиск по всем сообщениям"
            aria-label="Поиск по всем сообщениям"
            aria-controls={listId}
            role="combobox"
            aria-expanded={results.length > 0}
            aria-autocomplete="list"
            spellCheck={false}
            autoComplete="off"
            maxLength={200}
            className="min-w-0 flex-1 bg-transparent text-sm text-tg-text placeholder:text-tg-textFaint focus:outline-none"
            data-testid="search-input"
          />
          {query.length > 0 && (
            <button
              type="button"
              onClick={handleClear}
              aria-label="Очистить"
              title="Очистить"
              className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
            >
              <X size={14} />
            </button>
          )}
          <kbd className="shrink-0 rounded border border-tg-border bg-tg-elevated px-1.5 py-0.5 font-mono text-[10px] font-semibold text-tg-textFaint">
            Esc
          </kbd>
        </div>

        {/* ------------------------------------------------------- sender tabs */}
        <div role="tablist" aria-label="Кто отправил" className="flex shrink-0 gap-1 border-b border-tg-borderSoft px-2 py-1.5">
          {SEARCH_SENDER_FILTERS.map((value) => {
            const active = sender === value;
            return (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => setSearchSender(value)}
                className={`rounded-md px-2.5 py-1 text-2xs font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent ${
                  active
                    ? "bg-tg-accent/15 text-tg-accentHover shadow-[inset_0_0_0_1px_rgba(135,116,225,0.35)]"
                    : "text-tg-textMuted hover:bg-tg-hover hover:text-tg-text"
                }`}
              >
                {SENDER_LABELS[value]}
              </button>
            );
          })}
          <span className="ml-auto self-center pr-1 text-[10px] tabular text-tg-textFaint">
            {loading ? "поиск…" : results.length > 0 ? `${results.length} совпадений` : ""}
          </span>
        </div>

        {/* ------------------------------------------------------------- body */}
        <div
          id={listId}
          role="listbox"
          aria-label="Результаты поиска"
          className="min-h-0 flex-1 overflow-y-auto scroll-area px-1.5 py-1.5"
        >
          {showIdle && (
            <div className="flex flex-col items-center gap-2 px-4 py-12 text-center">
              <Search size={26} className="text-tg-textFaint" />
              <p className="text-xs text-tg-textMuted">
                Начните вводить текст — ищем по всем чатам, телефонам и TX-номерам.
              </p>
              <p className="text-2xs text-tg-textFaint">
                Поиск по подстроке: <span className="font-mono">4321</span> найдёт{" "}
                <span className="font-mono">+7 4321 …</span>
              </p>
            </div>
          )}

          {error !== null && (
            <div
              role="alert"
              data-testid="search-error"
              className="mx-1.5 my-2 flex items-start gap-2 rounded-lg border border-tg-danger/30 bg-tg-danger/10 px-3 py-2"
            >
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-tg-danger" />
              <div className="min-w-0">
                <p className="text-2xs font-semibold text-tg-danger">Поиск недоступен</p>
                <p className="mt-0.5 break-words text-2xs leading-4 text-tg-danger/90">{error}</p>
              </div>
            </div>
          )}

          {showEmpty && (
            <div className="flex flex-col items-center gap-2 px-4 py-12 text-center" data-testid="search-empty">
              <SearchX size={26} className="text-tg-textFaint" />
              <p className="text-xs text-tg-textMuted">Ничего не найдено</p>
              <p className="max-w-xs text-2xs text-tg-textFaint">
                Попробуйте короче — например, часть номера или кода.
              </p>
            </div>
          )}

          {loading && results.length === 0 && error === null && <SkeletonRows />}

          {groups.map((group) => (
            <section key={group.chatId} className="mb-1" aria-label={group.title}>
              <header className="flex items-center gap-2 px-2 py-1.5">
                <span
                  className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-[10px] font-bold text-white"
                  style={{ backgroundColor: avatarColor(group.title) }}
                  aria-hidden
                >
                  {initials(group.title)}
                </span>
                <span className="min-w-0 flex-1 truncate text-2xs font-semibold uppercase tracking-wide text-tg-textMuted">
                  {group.title || `Чат ${group.chatId}`}
                </span>
                {group.merchantCode && (
                  <span className="shrink-0 rounded border border-tg-border bg-tg-elevated px-1 font-mono text-[9px] uppercase tracking-wide text-tg-textFaint">
                    {group.merchantCode}
                  </span>
                )}
                <span className="shrink-0 rounded-full bg-tg-hover px-1.5 text-[10px] font-bold leading-4 text-tg-textMuted">
                  {group.hits.length}
                </span>
              </header>

              {group.hits.map((hit) => {
                const selected = sameKey(cursor, keyOf(hit));
                const outgoing = hit.message.is_outgoing;
                return (
                  <button
                    key={`${hit.chat_id}-${hit.message.id}`}
                    id={`search-hit-${hit.chat_id}-${hit.message.id}`}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    data-testid="search-hit"
                    data-message-id={hit.message.id}
                    data-chat-id={hit.chat_id}
                    onClick={() => void openHit(hit)}
                    disabled={opening}
                    className={`flex w-full flex-col gap-0.5 rounded-lg px-2.5 py-2 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent disabled:cursor-progress ${
                      selected ? "bg-tg-accent/20 shadow-[inset_0_0_0_1px_rgba(135,116,225,0.35)]" : "hover:bg-tg-hover/70"
                    }`}
                  >
                    <span className="flex items-center gap-1.5 text-[10px]">
                      <span className={outgoing ? "font-semibold text-tg-accentHover" : "font-semibold text-tg-success"}>
                        {senderLabel(hit)}
                      </span>
                      <span className="text-tg-textFaint" aria-hidden>
                        ·
                      </span>
                      <span className="text-tg-textFaint">{formatChatListTime(hit.message.created_at)}</span>
                      <span className="text-tg-textFaint">{formatClock(hit.message.created_at)}</span>
                      {hit.message.is_deleted_in_tg && (
                        <span className="rounded bg-tg-danger/15 px-1 text-[9px] font-bold uppercase text-tg-danger">
                          удалено
                        </span>
                      )}
                      {selected && (
                        <CornerDownLeft size={11} className="ml-auto shrink-0 text-tg-accentHover" aria-hidden />
                      )}
                    </span>
                    <span className="line-clamp-2 break-words text-xs leading-5 text-tg-text">
                      {highlightSnippet(hit.snippet, hit.match_spans, query)}
                    </span>
                  </button>
                );
              })}
            </section>
          ))}

          {results.length > 0 && (
            <p className="px-2 py-2 text-center text-[10px] text-tg-textFaint">
              Первые {results.length} совпадений · ↑↓ выбрать · Enter открыть
            </p>
          )}
        </div>

        {/* ----------------------------------------------------------- footer */}
        <div className="flex shrink-0 items-center justify-between border-t border-tg-border px-3 py-1.5 text-[10px] text-tg-textFaint">
          <span className="flex items-center gap-3">
            <span>
              <kbd className="rounded border border-tg-border bg-tg-elevated px-1 font-mono">↑</kbd>{" "}
              <kbd className="rounded border border-tg-border bg-tg-elevated px-1 font-mono">↓</kbd> навигация
            </span>
            <span>
              <kbd className="rounded border border-tg-border bg-tg-elevated px-1 font-mono">Enter</kbd> перейти
            </span>
          </span>
          {selectedHit && <span className="truncate pl-3">выбрано: {senderLabel(selectedHit)}</span>}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** Placeholder rows while the first page of a query is in flight. */
function SkeletonRows(): JSX.Element {
  return (
    <div className="space-y-1 p-1" aria-hidden>
      {Array.from({ length: 4 }).map((_, index) => (
        <div key={index} className="rounded-lg px-2.5 py-2">
          <div className="mb-1.5 h-2 w-24 animate-pulse rounded bg-tg-elevated" />
          <div className="h-2.5 w-full animate-pulse rounded bg-tg-elevated/70" />
        </div>
      ))}
    </div>
  );
}

export default SearchOverlay;
