import { AlertTriangle, Check, Loader2, Plus, RefreshCw, Save, Trash2, X } from "lucide-react";
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

import type { QuickReply } from "../lib/types";
import {
  QUICK_REPLY_BODY_MAX_LENGTH,
  QUICK_REPLY_CATEGORY_MAX_LENGTH,
  QUICK_REPLY_DEFAULT_CATEGORY,
  QUICK_REPLY_SHORTCUT_MAX_LENGTH,
  QUICK_REPLY_SHORTCUT_PATTERN,
  QUICK_REPLY_TITLE_MAX_LENGTH,
} from "../lib/types";
import { useCRMStore } from "../store/useCRMStore";

export interface QuickReplyEditorProps {
  /** `false` unmounts the portal; the parent keeps the open flag in its own state. */
  open: boolean;
  onClose: () => void;
}

/** Which row the form is currently bound to: `"new"`, an existing id, or nothing. */
type EditorTarget = number | "new" | null;

interface DraftFields {
  shortcut: string;
  title: string;
  body: string;
  category: string;
  sort_order: string;
  is_active: boolean;
}

const EMPTY_DRAFT: DraftFields = {
  shortcut: "",
  title: "",
  body: "",
  category: QUICK_REPLY_DEFAULT_CATEGORY,
  sort_order: "0",
  is_active: true,
};

/** `sort_order` is edited as a string so a half-typed "-" or "" stays representable. */
const SORT_ORDER_PATTERN = /^-?\d{0,7}$/;

function draftFromReply(reply: QuickReply): DraftFields {
  return {
    shortcut: reply.shortcut,
    title: reply.title,
    body: reply.body,
    category: reply.category || QUICK_REPLY_DEFAULT_CATEGORY,
    sort_order: String(reply.sort_order),
    is_active: reply.is_active,
  };
}

/**
 * Client-side mirror of the store/backend validators (§5.2).
 *
 * Returning the message *and* the field it belongs to lets the form mark the
 * exact input instead of one generic banner above the whole dialog.
 */
function validateDraft(
  draft: DraftFields,
  siblings: readonly QuickReply[],
  selfId: EditorTarget,
): { field: keyof DraftFields | null; message: string } | null {
  const shortcut = draft.shortcut.trim().toLowerCase();
  const title = draft.title.trim();
  const category = draft.category.trim();

  if (!shortcut) return { field: "shortcut", message: "Укажите короткий код." };
  if (shortcut.length > QUICK_REPLY_SHORTCUT_MAX_LENGTH) {
    return { field: "shortcut", message: `Не длиннее ${QUICK_REPLY_SHORTCUT_MAX_LENGTH} символов.` };
  }
  if (!QUICK_REPLY_SHORTCUT_PATTERN.test(shortcut)) {
    return { field: "shortcut", message: "Только латинские буквы, цифры, «-» и «_»." };
  }
  const alias = siblings.find(
    (item) => item.id !== selfId && item.shortcut.trim().toLowerCase() === shortcut,
  );
  if (alias) return { field: "shortcut", message: `Код «${shortcut}» уже занят.` };

  if (!title) return { field: "title", message: "Укажите подпись." };
  if (title.length > QUICK_REPLY_TITLE_MAX_LENGTH) {
    return { field: "title", message: `Не длиннее ${QUICK_REPLY_TITLE_MAX_LENGTH} символов.` };
  }

  if (!draft.body.trim()) return { field: "body", message: "Текст не может быть пустым." };
  if (draft.body.length > QUICK_REPLY_BODY_MAX_LENGTH) {
    return { field: "body", message: `Не длиннее ${QUICK_REPLY_BODY_MAX_LENGTH} символов.` };
  }

  if (category.length > QUICK_REPLY_CATEGORY_MAX_LENGTH) {
    return { field: "category", message: `Не длиннее ${QUICK_REPLY_CATEGORY_MAX_LENGTH} символов.` };
  }

  if (!SORT_ORDER_PATTERN.test(draft.sort_order.trim())) {
    return { field: "sort_order", message: "Порядок — целое число." };
  }

  return null;
}

/**
 * Quick-reply CRUD editor (CONTRACTS_V5 §5).
 *
 * The dialog is a pure view over the store slice: it never keeps an authoritative
 * copy of the list (so a §5.3 `QUICK_REPLIES_UPDATED` frame refreshes it live),
 * only the form of the row being edited. Saves call the store and observe the
 * returned record — a `null` means the store already toasted the reason, which is
 * mirrored inline next to the form instead of only in the transient toast.
 *
 * Portal hygiene follows `ForwardDialog`: one effect owns the Escape/Tab listeners
 * and the body scroll lock, its cleanup restores the *saved* overflow value, and
 * the close callback is read through a ref so a fresh arrow never re-runs it.
 */
export function QuickReplyEditor({ open, onClose }: QuickReplyEditorProps): JSX.Element | null {
  const replies = useCRMStore((state) => state.quickReplies);
  const loading = useCRMStore((state) => state.quickRepliesLoading);
  const storeError = useCRMStore((state) => state.quickRepliesError);
  const mutating = useCRMStore((state) => state.quickReplyMutating);
  const pendingId = useCRMStore((state) => state.quickReplyPendingId);
  const loadQuickReplies = useCRMStore((state) => state.loadQuickReplies);
  const createQuickReply = useCRMStore((state) => state.createQuickReply);
  const updateQuickReply = useCRMStore((state) => state.updateQuickReply);
  const deleteQuickReply = useCRMStore((state) => state.deleteQuickReply);

  const titleId = useId();
  const hintId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const shortcutRef = useRef<HTMLInputElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  const [target, setTarget] = useState<EditorTarget>(null);
  const [draft, setDraft] = useState<DraftFields>(EMPTY_DRAFT);
  const [localError, setLocalError] = useState<{ field: keyof DraftFields | null; message: string } | null>(null);
  /** Two-step delete: the id awaiting the second click (an accidental click is cheap). */
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  /* ------------------------------------------------------- derived values */
  const selected = useMemo(
    () => (typeof target === "number" ? (replies.find((item) => item.id === target) ?? null) : null),
    [replies, target],
  );

  const isDirty = useMemo(() => {
    if (target === "new") {
      return (
        draft.shortcut !== EMPTY_DRAFT.shortcut ||
        draft.title !== EMPTY_DRAFT.title ||
        draft.body !== EMPTY_DRAFT.body ||
        draft.category !== EMPTY_DRAFT.category ||
        draft.sort_order !== EMPTY_DRAFT.sort_order
      );
    }
    if (!selected) return false;
    const initial = draftFromReply(selected);
    return (
      draft.shortcut !== initial.shortcut ||
      draft.title !== initial.title ||
      draft.body !== initial.body ||
      draft.category !== initial.category ||
      draft.sort_order !== initial.sort_order ||
      draft.is_active !== initial.is_active
    );
  }, [draft, selected, target]);

  const canSave =
    target !== null &&
    !mutating &&
    draft.body.trim().length > 0 &&
    draft.title.trim().length > 0 &&
    draft.shortcut.trim().length > 0 &&
    (target === "new" || isDirty);

  /* ---------------------------------------------------- mount / reload / focus */
  useEffect(() => {
    if (!open) return;
    void loadQuickReplies({ force: true });
  }, [open, loadQuickReplies]);

  useEffect(() => {
    if (!open) return;

    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => shortcutRef.current?.focus(), 0);

    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      const restoreTo = restoreFocusRef.current;
      if (restoreTo && document.contains(restoreTo)) restoreTo.focus({ preventScroll: true });
      restoreFocusRef.current = null;
    };
  }, [open]);

  /* Closing resets the transient state so a reopen starts clean. */
  useEffect(() => {
    if (open) return;
    setTarget(null);
    setDraft(EMPTY_DRAFT);
    setLocalError(null);
    setConfirmDeleteId(null);
  }, [open]);

  /* A row deleted elsewhere must not stay bound to the form. */
  useEffect(() => {
    if (typeof target !== "number") return;
    if (replies.some((item) => item.id === target)) return;
    setTarget(null);
    setDraft(EMPTY_DRAFT);
    setLocalError(null);
  }, [replies, target]);

  /* ------------------------------------------------------- keyboard */
  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        /* Capture phase: the composer's own Escape handler must not eat the key. */
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const container = dialogRef.current;
      if (!container) return;
      const focusable = Array.from(
        container.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
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

    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [open]);

  /* ------------------------------------------------------------ handlers */
  const startNew = useCallback(() => {
    const highest = replies.reduce((max, item) => Math.max(max, item.sort_order), 0);
    setTarget("new");
    setDraft({ ...EMPTY_DRAFT, sort_order: String(highest + 10) });
    setLocalError(null);
    setConfirmDeleteId(null);
    window.setTimeout(() => shortcutRef.current?.focus(), 0);
  }, [replies]);

  const selectReply = useCallback((reply: QuickReply) => {
    setTarget(reply.id);
    setDraft(draftFromReply(reply));
    setLocalError(null);
    setConfirmDeleteId(null);
  }, []);

  const patchDraft = useCallback((patch: Partial<DraftFields>) => {
    setDraft((current) => ({ ...current, ...patch }));
    setLocalError(null);
  }, []);

  const handleShortcut = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    /* Lower-case on input: the backend normalises case, and showing the value in
       the form the way it will be stored avoids a confusing "duplicate" error. */
    patchDraft({ shortcut: event.target.value.toLowerCase().slice(0, QUICK_REPLY_SHORTCUT_MAX_LENGTH) });
  }, [patchDraft]);

  const handleTitle = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    patchDraft({ title: event.target.value.slice(0, QUICK_REPLY_TITLE_MAX_LENGTH) });
  }, [patchDraft]);

  const handleBody = useCallback((event: ChangeEvent<HTMLTextAreaElement>) => {
    patchDraft({ body: event.target.value.slice(0, QUICK_REPLY_BODY_MAX_LENGTH) });
  }, [patchDraft]);

  const handleCategory = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    patchDraft({ category: event.target.value.slice(0, QUICK_REPLY_CATEGORY_MAX_LENGTH) });
  }, [patchDraft]);

  const handleSortOrder = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    patchDraft({ sort_order: event.target.value });
  }, [patchDraft]);

  const handleBackdropMouseDown = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    event.preventDefault();
    onCloseRef.current();
  }, []);

  const handleCardMouseDown = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    event.stopPropagation();
  }, []);

  const handleSave = useCallback(async () => {
    if (target === null) return;
    const problem = validateDraft(draft, replies, target);
    if (problem) {
      setLocalError(problem);
      return;
    }
    const sortOrder = Number(draft.sort_order.trim() || "0");
    if (target === "new") {
      const created = await createQuickReply({
        shortcut: draft.shortcut.trim().toLowerCase(),
        title: draft.title.trim(),
        body: draft.body,
        category: draft.category.trim() || QUICK_REPLY_DEFAULT_CATEGORY,
        sort_order: Number.isFinite(sortOrder) ? sortOrder : 0,
        is_active: draft.is_active,
      });
      if (created) {
        setTarget(created.id);
        setDraft(draftFromReply(created));
        setLocalError(null);
      }
      return;
    }
    const updated = await updateQuickReply(target, {
      shortcut: draft.shortcut.trim().toLowerCase(),
      title: draft.title.trim(),
      body: draft.body,
      category: draft.category.trim() || QUICK_REPLY_DEFAULT_CATEGORY,
      sort_order: Number.isFinite(sortOrder) ? sortOrder : 0,
      is_active: draft.is_active,
    });
    if (updated) {
      setDraft(draftFromReply(updated));
      setLocalError(null);
    }
  }, [createQuickReply, draft, replies, target, updateQuickReply]);

  const handleDelete = useCallback(
    async (reply: QuickReply) => {
      if (mutating) return;
      if (confirmDeleteId !== reply.id) {
        setConfirmDeleteId(reply.id);
        return;
      }
      setConfirmDeleteId(null);
      const removed = await deleteQuickReply(reply.id);
      if (removed && target === reply.id) {
        setTarget(null);
        setDraft(EMPTY_DRAFT);
        setLocalError(null);
      }
    },
    [confirmDeleteId, deleteQuickReply, mutating, target],
  );

  if (!open) return null;
  if (typeof document === "undefined") return null;

  const fieldErrors = localError;

  return createPortal(
    <div
      className="fixed inset-0 z-[120] flex animate-fade-in items-center justify-center p-4"
      style={{ backgroundColor: "rgba(0, 0, 0, 0.7)" }}
      onMouseDown={handleBackdropMouseDown}
      data-testid="quick-reply-editor"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex max-h-[90vh] w-full max-w-2xl animate-slide-up flex-col overflow-hidden rounded-xl border border-tg-border bg-tg-panel shadow-pop"
        onMouseDown={handleCardMouseDown}
      >
        {/* header */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-tg-border px-4 py-3">
          <div className="min-w-0">
            <h2 id={titleId} className="truncate text-sm font-semibold text-tg-text">
              Быстрые ответы
            </h2>
            <p className="truncate text-[10px] text-tg-textFaint">
              Список хранится на сервере и обновляется у всех операторов сразу.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => void loadQuickReplies({ force: true })}
              disabled={loading || mutating}
              className="grid h-7 w-7 place-items-center rounded-full text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent disabled:cursor-not-allowed disabled:opacity-40"
              aria-label="Обновить список быстрых ответов"
              title="Обновить список"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            </button>
            <button
              type="button"
              onClick={() => onCloseRef.current()}
              className="grid h-7 w-7 place-items-center rounded-full text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
              aria-label="Закрыть редактор быстрых ответов"
              title="Закрыть (Esc)"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto scroll-area px-4 py-3 md:flex-row">
          {/* ------------------------------------------------------------ list */}
          <section className="flex min-h-0 flex-col md:w-[38%] md:shrink-0" aria-label="Список быстрых ответов">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-2xs uppercase tracking-wider text-tg-textFaint">
                Ответы · {replies.length}
              </span>
              <button
                type="button"
                onClick={startNew}
                disabled={mutating}
                className="inline-flex items-center gap-1 rounded-lg bg-tg-accent/15 px-2 py-1 text-2xs font-medium text-tg-accentHover transition hover:bg-tg-accent/25 focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent disabled:cursor-not-allowed disabled:opacity-40"
                aria-label="Добавить быстрый ответ"
                title="Добавить быстрый ответ"
              >
                <Plus size={12} /> Добавить
              </button>
            </div>

            <div className="max-h-56 min-h-[96px] space-y-1 overflow-y-auto rounded-lg bg-tg-elevated/60 p-1 md:max-h-none md:flex-1">
              {loading && replies.length === 0 ? (
                <p className="flex items-center justify-center gap-1.5 px-1 py-4 text-center text-2xs text-tg-textFaint">
                  <Loader2 size={12} className="animate-spin" /> Загрузка…
                </p>
              ) : replies.length === 0 ? (
                <p className="px-1 py-4 text-center text-2xs text-tg-textFaint">
                  Пока нет ни одного быстрого ответа — нажмите «Добавить».
                </p>
              ) : (
                replies.map((reply) => {
                  const active = target === reply.id;
                  const busy = pendingId === reply.id;
                  return (
                    <div
                      key={reply.id}
                      className={`flex items-start gap-1 rounded-lg border px-2 py-1.5 transition-colors ${
                        active ? "border-tg-accent bg-tg-accent/15" : "border-transparent hover:bg-tg-hover/70"
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => selectReply(reply)}
                        aria-pressed={active}
                        aria-label={`Редактировать быстрый ответ «${reply.title}»`}
                        className="min-w-0 flex-1 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
                      >
                        <span className="flex items-center gap-1">
                          <span className="truncate text-xs font-semibold text-tg-text">
                            {reply.title || reply.shortcut}
                          </span>
                          {!reply.is_active && (
                            <span className="shrink-0 rounded bg-tg-hover px-1 text-[9px] font-bold uppercase text-tg-textFaint">
                              off
                            </span>
                          )}
                          {busy && <Loader2 size={10} className="shrink-0 animate-spin text-tg-accentHover" />}
                        </span>
                        <span className="block truncate font-mono text-[10px] text-tg-textFaint">
                          /{reply.shortcut}
                        </span>
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleDelete(reply)}
                        disabled={mutating}
                        className={`grid h-6 shrink-0 place-items-center rounded px-1 text-[10px] font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-danger disabled:cursor-not-allowed disabled:opacity-40 ${
                          confirmDeleteId === reply.id
                            ? "bg-tg-danger/20 text-tg-danger"
                            : "text-tg-textFaint hover:bg-tg-hover hover:text-tg-danger"
                        }`}
                        aria-label={
                          confirmDeleteId === reply.id
                            ? `Подтвердить удаление «${reply.title}»`
                            : `Удалить быстрый ответ «${reply.title}»`
                        }
                        title={confirmDeleteId === reply.id ? "Нажмите ещё раз для подтверждения" : "Удалить"}
                      >
                        {confirmDeleteId === reply.id ? "Точно?" : <Trash2 size={12} />}
                      </button>
                    </div>
                  );
                })
              )}
            </div>
          </section>

          {/* ------------------------------------------------------------ form */}
          <section className="flex min-w-0 flex-1 flex-col gap-2.5" aria-label="Форма быстрого ответа">
            {(storeError || fieldErrors) && (
              <div
                role="alert"
                className="flex items-start gap-1.5 rounded-lg border border-tg-danger/30 bg-tg-danger/10 px-2.5 py-1.5 text-2xs leading-4 text-tg-danger"
              >
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                <span>{fieldErrors?.message ?? storeError}</span>
              </div>
            )}

            {target === null ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-tg-border px-3 py-6 text-center">
                <p className="text-2xs font-medium text-tg-textMuted">Выберите ответ слева</p>
                <p className="text-[10px] text-tg-textFaint">
                  или создайте новый — он сразу появится в панели быстрых ответов.
                </p>
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <label className="flex flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wider text-tg-textFaint">Код</span>
                    <input
                      ref={shortcutRef}
                      type="text"
                      value={draft.shortcut}
                      onChange={handleShortcut}
                      disabled={mutating}
                      maxLength={QUICK_REPLY_SHORTCUT_MAX_LENGTH}
                      placeholder="tx"
                      aria-label="Короткий код быстрого ответа"
                      aria-invalid={fieldErrors?.field === "shortcut"}
                      className="w-full rounded-lg border border-tg-border bg-tg-elevated px-2 py-1.5 font-mono text-xs text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wider text-tg-textFaint">Порядок</span>
                    <input
                      type="text"
                      inputMode="numeric"
                      value={draft.sort_order}
                      onChange={handleSortOrder}
                      disabled={mutating}
                      aria-label="Порядок сортировки"
                      aria-invalid={fieldErrors?.field === "sort_order"}
                      className="w-full rounded-lg border border-tg-border bg-tg-elevated px-2 py-1.5 text-xs tabular text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                    />
                  </label>
                </div>

                <label className="flex flex-col gap-1">
                  <span className="text-[10px] uppercase tracking-wider text-tg-textFaint">Подпись</span>
                  <input
                    type="text"
                    value={draft.title}
                    onChange={handleTitle}
                    disabled={mutating}
                    maxLength={QUICK_REPLY_TITLE_MAX_LENGTH}
                    placeholder="Запросить TX ID"
                    aria-label="Подпись быстрого ответа"
                    aria-invalid={fieldErrors?.field === "title"}
                    className="w-full rounded-lg border border-tg-border bg-tg-elevated px-2 py-1.5 text-xs text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-[10px] uppercase tracking-wider text-tg-textFaint">Категория</span>
                  <input
                    type="text"
                    value={draft.category}
                    onChange={handleCategory}
                    disabled={mutating}
                    maxLength={QUICK_REPLY_CATEGORY_MAX_LENGTH}
                    placeholder={QUICK_REPLY_DEFAULT_CATEGORY}
                    aria-label="Категория быстрого ответа"
                    aria-invalid={fieldErrors?.field === "category"}
                    className="w-full rounded-lg border border-tg-border bg-tg-elevated px-2 py-1.5 text-xs text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                  />
                </label>

                <label className="flex min-h-0 flex-col gap-1">
                  <span className="text-[10px] uppercase tracking-wider text-tg-textFaint">Текст</span>
                  <textarea
                    value={draft.body}
                    onChange={handleBody}
                    disabled={mutating}
                    rows={5}
                    maxLength={QUICK_REPLY_BODY_MAX_LENGTH}
                    placeholder="Текст, который будет вставлен в поле ввода"
                    aria-label="Текст быстрого ответа"
                    aria-invalid={fieldErrors?.field === "body"}
                    aria-describedby={hintId}
                    className="max-h-52 w-full resize-none rounded-lg border border-tg-border bg-tg-elevated px-2.5 py-2 text-xs leading-5 text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                  />
                  <span id={hintId} className="text-right text-[10px] tabular text-tg-textFaint">
                    {draft.body.length} / {QUICK_REPLY_BODY_MAX_LENGTH}
                  </span>
                </label>

                <label className="flex items-center gap-2 text-2xs text-tg-textMuted">
                  <input
                    type="checkbox"
                    checked={draft.is_active}
                    onChange={(event) => patchDraft({ is_active: event.target.checked })}
                    disabled={mutating}
                    className="h-3.5 w-3.5 accent-tg-accent"
                    aria-label="Быстрый ответ активен"
                  />
                  Активен — показывать в панели быстрых ответов
                </label>

                {selected?.created_by && (
                  <p className="text-[10px] text-tg-textFaint">Создал: {selected.created_by}</p>
                )}

                <div className="mt-auto flex items-center justify-end gap-2 pt-1">
                  {target !== "new" && selected && (
                    <button
                      type="button"
                      onClick={() => void handleDelete(selected)}
                      disabled={mutating}
                      className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-danger disabled:cursor-not-allowed disabled:opacity-40 ${
                        confirmDeleteId === selected.id
                          ? "border-tg-danger bg-tg-danger/20 text-tg-danger"
                          : "border-tg-border text-tg-textMuted hover:border-tg-danger/50 hover:text-tg-danger"
                      }`}
                      aria-label={
                        confirmDeleteId === selected.id
                          ? "Подтвердить удаление быстрого ответа"
                          : "Удалить быстрый ответ"
                      }
                    >
                      <Trash2 size={12} />
                      {confirmDeleteId === selected.id ? "Подтвердить" : "Удалить"}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void handleSave()}
                    disabled={!canSave}
                    className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-white transition focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent ${
                      canSave ? "bg-tg-accent hover:bg-tg-accentHover" : "cursor-not-allowed bg-tg-accent/40 text-white/60"
                    }`}
                    aria-busy={mutating}
                    aria-label={target === "new" ? "Создать быстрый ответ" : "Сохранить быстрый ответ"}
                  >
                    {mutating ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : target === "new" ? (
                      <Plus size={13} />
                    ) : (
                      <Save size={13} />
                    )}
                    {target === "new" ? "Создать" : "Сохранить"}
                  </button>
                </div>
              </>
            )}
          </section>
        </div>

        {/* footer */}
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-tg-border px-4 py-3">
          <span className="flex items-center gap-1 text-[10px] text-tg-textFaint">
            <Check size={11} className="text-tg-success" />
            Изменения сразу видны всем операторам
          </span>
          <button
            type="button"
            onClick={() => onCloseRef.current()}
            className="rounded-lg px-3 py-1.5 text-xs font-medium text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
            aria-label="Закрыть редактор"
          >
            Готово
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export default QuickReplyEditor;
