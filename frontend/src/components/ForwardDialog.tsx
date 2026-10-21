import { Check, Loader2, Search, ShieldAlert, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { createPortal } from "react-dom";

import { attachmentIcon, initials } from "../lib/format";
import type { ChatListItem, Message } from "../lib/types";

export interface ForwardDialogProps {
  sourceMessage: Message;
  chats: ChatListItem[];
  currentChatId: number;
  onClose: () => void;
  onConfirm: (targetChatId: number, comment?: string) => Promise<void>;
  submitting?: boolean;
  error?: string | null;
}

/** Telegram caption cap — the backend rejects longer forward comments (CONTRACTS_V3 §1). */
const MAX_COMMENT_LENGTH = 1024;
/** Preview body cap; a longer source text is cut and marked with an ellipsis. */
const PREVIEW_LIMIT = 120;

/**
 * Deterministic avatar palette. `ChatListItem` carries no colour field yet, so the
 * tint is derived from the chat id — stable across renders.
 */
const AVATAR_COLORS = [
  "#8774e1",
  "#2b5278",
  "#3f7a5a",
  "#8a5a2b",
  "#7a2b52",
  "#2b5a7a",
  "#5a7a2b",
  "#5a2b7a",
] as const;

function avatarColorForId(id: number): string {
  const safe = Number.isFinite(id) ? Math.abs(Math.trunc(id)) : 0;
  return AVATAR_COLORS[safe % AVATAR_COLORS.length] ?? AVATAR_COLORS[0];
}

/** First two letters of the title, uppercased («Мерчант Альфа» → «МЕ»). */
function avatarInitials(title: string): string {
  const trimmed = (title ?? "").trim();
  return trimmed ? initials(trimmed) : "?";
}

/** Single-line body of the source message, cut to `PREVIEW_LIMIT` characters. */
function previewBody(message: Message): string {
  const raw = (message.caption?.trim() || message.text?.trim() || "").replace(/\s+/g, " ");
  if (!raw) return "(без текста)";
  return raw.length > PREVIEW_LIMIT ? `${raw.slice(0, PREVIEW_LIMIT).trimEnd()}…` : raw;
}

/**
 * Forward dialog (CONTRACTS_V3 §1, §3.1).
 *
 * Forwards the source message to another chat *from our own account*: the original
 * sender is never revealed, so the dialog carries a permanent warning instead of a
 * one-off toast. The parent owns the request lifecycle — this component only reports
 * the chosen target through `onConfirm` and never closes itself, so a failed send
 * keeps the user's selection and comment intact for a retry.
 *
 * Listener and scroll-lock hygiene:
 * - `Escape` / focus-trap listeners live in one effect with a matching cleanup;
 * - callbacks are read through refs so a new inline arrow from the parent never
 *   re-runs that effect (a second cleanup would restore the *locked* overflow value
 *   and freeze the page behind the dialog);
 * - `document.body.style.overflow` is saved before the lock and restored from the
 *   saved value on unmount.
 */
export function ForwardDialog({
  sourceMessage,
  chats,
  currentChatId,
  onClose,
  onConfirm,
  submitting = false,
  error = null,
}: ForwardDialogProps): JSX.Element | null {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [comment, setComment] = useState("");

  const titleId = useId();
  const warningId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  /* Latest callbacks/flags behind refs — the effects below subscribe once. */
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onConfirmRef = useRef(onConfirm);
  onConfirmRef.current = onConfirm;
  const submittingRef = useRef(submitting);
  submittingRef.current = submitting;
  const selectedRef = useRef<number | null>(selectedId);
  selectedRef.current = selectedId;
  const commentRef = useRef(comment);
  commentRef.current = comment;

  const isOpen = sourceMessage !== null && sourceMessage !== undefined;

  /* ------------------------------------------------------- derived lists */
  const availableChats = useMemo(
    () => (Array.isArray(chats) ? chats.filter((chat) => chat.id !== currentChatId) : []),
    [chats, currentChatId],
  );

  const visibleChats = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return availableChats;
    return availableChats.filter(
      (chat) =>
        chat.title?.toLowerCase().includes(needle) ||
        (chat.merchant_code?.toLowerCase().includes(needle) ?? false),
    );
  }, [availableChats, query]);

  /* A chat that vanished from the list must not stay selected invisibly. */
  const effectiveSelectedId =
    selectedId !== null && availableChats.some((chat) => chat.id === selectedId) ? selectedId : null;

  const attachment = sourceMessage?.attachments?.[0] ?? null;
  const extraAttachments = Math.max((sourceMessage?.attachments?.length ?? 0) - 1, 0);

  const canSubmit = effectiveSelectedId !== null && !submitting;

  /* ------------------------------------------- mount / focus / scroll lock */
  useEffect(() => {
    if (!isOpen) return;

    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    /* Autofocus the search box once the portal is attached. */
    const focusTimer = window.setTimeout(() => searchRef.current?.focus(), 0);

    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      const restoreTo = restoreFocusRef.current;
      if (restoreTo && document.contains(restoreTo)) {
        restoreTo.focus({ preventScroll: true });
      }
      restoreFocusRef.current = null;
    };
  }, [isOpen]);

  /* -------------------------------------------------- keyboard + backdrop */
  const requestClose = useCallback(() => {
    if (submittingRef.current) return;
    onCloseRef.current();
  }, []);

  useEffect(() => {
    if (!isOpen) return;

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        /* A send is in flight: swallow the key so the request is not abandoned. */
        event.preventDefault();
        event.stopPropagation();
        if (submittingRef.current) return;
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      /* Minimal focus trap: the chat behind the overlay must stay unreachable. */
      const container = dialogRef.current;
      if (!container) return;
      const focusable = Array.from(
        container.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.offsetParent !== null);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      }
    };

    /* Capture phase: win against the composer's Escape handler in the chat below. */
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [isOpen]);

  /* ------------------------------------------------------------- handlers */
  const handleBackdropMouseDown = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    /* Only the backdrop itself closes; anything inside the card keeps the click. */
    if (event.target !== event.currentTarget) return;
    if (submittingRef.current) return;
    event.preventDefault();
    onCloseRef.current();
  }, []);

  const handleCardMouseDown = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    event.stopPropagation();
  }, []);

  const handleQueryChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setQuery(event.target.value);
  }, []);

  const handleCommentChange = useCallback((event: ChangeEvent<HTMLTextAreaElement>) => {
    setComment(event.target.value.slice(0, MAX_COMMENT_LENGTH));
  }, []);

  const handleSelect = useCallback((chatId: number) => {
    if (submittingRef.current) return;
    setSelectedId(chatId);
  }, []);

  const handleSubmit = useCallback(() => {
    const targetId = selectedRef.current;
    if (targetId === null || submittingRef.current) return;
    if (!availableChats.some((chat) => chat.id === targetId)) return;
    const trimmed = commentRef.current.trim();
    /* Never close on success — the parent dismisses the dialog after refreshing. */
    void Promise.resolve(onConfirmRef.current(targetId, trimmed || undefined)).catch(() => {
      /* The failure is surfaced through the `error` prop; the dialog stays open. */
    });
  }, [availableChats]);

  if (!isOpen) return null;
  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[120] flex animate-fade-in items-center justify-center p-4"
      style={{ backgroundColor: "rgba(0, 0, 0, 0.7)" }}
      onMouseDown={handleBackdropMouseDown}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex max-h-[90vh] w-full max-w-md animate-slide-up flex-col overflow-hidden rounded-xl border border-tg-border bg-tg-panel shadow-pop"
        onMouseDown={handleCardMouseDown}
      >
        {/* header */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-tg-border px-4 py-3">
          <h2 id={titleId} className="truncate text-sm font-semibold text-tg-text">
            Переслать сообщение
          </h2>
          <button
            type="button"
            onClick={requestClose}
            disabled={submitting}
            className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="Закрыть"
            title="Закрыть (Esc)"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto scroll-area px-4 py-3">
          {/* source preview */}
          <section className="rounded-lg bg-tg-elevated p-3" aria-label="Исходное сообщение">
            {attachment && (
              <div className="mb-1.5 flex items-center gap-1.5">
                <span className="shrink-0 text-xs" aria-hidden>
                  {attachmentIcon(attachment.kind)}
                </span>
                <span className="min-w-0 flex-1 truncate text-2xs font-medium text-tg-text">
                  {attachment.file_name?.trim() || "Вложение"}
                </span>
                {extraAttachments > 0 && (
                  <span className="shrink-0 rounded-full bg-tg-hover px-1.5 py-[1px] text-[10px] font-semibold leading-4 text-tg-textMuted">
                    +{extraAttachments}
                  </span>
                )}
              </div>
            )}
            {/* `line-clamp-3` is not defined in index.css — two lines is what exists. */}
            <p className="line-clamp-2 break-words text-xs leading-5 text-tg-textMuted">
              {previewBody(sourceMessage)}
            </p>
          </section>

          {/* sender anonymity warning — the core promise of this feature */}
          <div
            id={warningId}
            role="note"
            className="flex items-start gap-2 rounded-lg border border-tg-warning/30 bg-tg-warning/10 px-3 py-2"
          >
            <ShieldAlert size={15} className="mt-0.5 shrink-0 text-tg-warning" />
            <p className="text-2xs leading-4 text-tg-warning">
              Будет отправлено от имени нашего аккаунта. Имя исходного отправителя скрыто.
            </p>
          </div>

          {error && (
            <div
              role="alert"
              className="rounded-lg border border-tg-danger/30 bg-tg-danger/10 px-3 py-2 text-2xs leading-4 text-tg-danger"
            >
              {error}
            </div>
          )}

          {/* search */}
          <div className="relative">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-tg-textFaint"
            />
            <input
              ref={searchRef}
              type="text"
              value={query}
              onChange={handleQueryChange}
              disabled={submitting}
              placeholder="Поиск чата…"
              aria-label="Поиск чата"
              className="w-full rounded-lg border border-tg-border bg-tg-elevated py-2 pl-8 pr-2.5 text-xs text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>

          {/* chat list */}
          <div
            className="max-h-64 space-y-1 overflow-y-auto scroll-area"
            role="group"
            aria-label="Список чатов"
          >
            {availableChats.length === 0 ? (
              <p className="px-1 py-4 text-center text-2xs text-tg-textFaint">Нет доступных чатов</p>
            ) : visibleChats.length === 0 ? (
              <p className="px-1 py-4 text-center text-2xs text-tg-textFaint">Чаты не найдены</p>
            ) : (
              visibleChats.map((chat) => {
                const selected = effectiveSelectedId === chat.id;
                return (
                  <button
                    key={chat.id}
                    type="button"
                    onClick={() => handleSelect(chat.id)}
                    disabled={submitting}
                    aria-pressed={selected}
                    className={`flex w-full items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent disabled:cursor-not-allowed ${
                      selected
                        ? "border-tg-accent bg-tg-accent/20"
                        : "border-transparent hover:bg-tg-hover/70"
                    }`}
                  >
                    <span
                      className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-[11px] font-bold text-white"
                      style={{ backgroundColor: avatarColorForId(chat.id) }}
                      aria-hidden
                    >
                      {avatarInitials(chat.title)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-semibold text-tg-text">{chat.title}</span>
                      {chat.merchant_code && (
                        <span className="block truncate text-2xs text-tg-textMuted">{chat.merchant_code}</span>
                      )}
                    </span>
                    {selected && <Check size={14} className="shrink-0 text-tg-accentHover" />}
                  </button>
                );
              })
            )}
          </div>

          {/* optional comment */}
          <div>
            <textarea
              value={comment}
              onChange={handleCommentChange}
              disabled={submitting}
              maxLength={MAX_COMMENT_LENGTH}
              rows={2}
              placeholder="Добавить комментарий (необязательно)"
              aria-label="Комментарий к пересылке"
              className="max-h-28 w-full resize-none rounded-lg border border-tg-border bg-tg-elevated px-2.5 py-2 text-xs leading-5 text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
            />
            <div className="mt-1 text-right text-[10px] tabular text-tg-textFaint">
              {comment.length} / {MAX_COMMENT_LENGTH}
            </div>
          </div>
        </div>

        {/* footer */}
        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-tg-border px-4 py-3">
          <button
            type="button"
            onClick={requestClose}
            disabled={submitting}
            className="rounded-lg px-3 py-1.5 text-xs font-medium text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent disabled:cursor-not-allowed disabled:opacity-40"
          >
            Отмена
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={`inline-flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-medium text-white transition focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent ${
              canSubmit
                ? "bg-tg-accent hover:bg-tg-accentHover"
                : "cursor-not-allowed bg-tg-accent/40 text-white/60"
            }`}
            aria-busy={submitting}
          >
            {submitting && <Loader2 size={13} className="animate-spin" />}
            {submitting ? "Отправка…" : "Переслать"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export default ForwardDialog;
