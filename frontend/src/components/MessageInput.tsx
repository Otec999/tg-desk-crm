import {
  Check,
  CornerUpLeft,
  FileText,
  Loader2,
  Lock,
  Pencil,
  Send,
  Settings2,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";

import { previewText, formatBytes } from "../lib/format";
import type { Message, QuickReply } from "../lib/types";
import { selectActiveQuickReplies, useCRMStore } from "../store/useCRMStore";
import { FilePicker } from "./FilePicker";
import { QuickReplyEditor } from "./QuickReplyEditor";

const MAX_LENGTH = 4096;
/** Telegram caption cap (CONTRACTS_V2 §3.1) — must match the store's limit. */
const MAX_MEDIA_CAPTION_LENGTH = 1024;

/**
 * CONTRACTS_V5 §5.4 — the canned replies now live in `quick_replies` and arrive
 * through the store. The former hard-coded `QUICK_REPLIES` constant is gone: the
 * backend seeds those same four texts on first start (idempotently, by shortcut),
 * so the UI must not carry a second source of truth (invariant §8.9).
 */

/**
 * Insert `snippet` into `value` at the caret, replacing any selection.
 *
 * §5.4 asks for positional insertion instead of always appending. Two details make
 * this feel natural: a separator newline is added only when the neighbouring
 * characters need one (appending to an empty draft must not start with a blank
 * line), and the caret is returned *after* the snippet so the operator keeps
 * typing where the text ended.
 */
function insertAtCaret(value: string, selectionStart: number, selectionEnd: number, snippet: string): { text: string; caret: number } {
  const start = Math.max(0, Math.min(selectionStart, value.length));
  const end = Math.max(start, Math.min(selectionEnd, value.length));
  const before = value.slice(0, start);
  const after = value.slice(end);

  const needsLeadingBreak = before.length > 0 && !before.endsWith("\n");
  const needsTrailingBreak = after.length > 0 && !after.startsWith("\n");
  const insertion = `${needsLeadingBreak ? "\n" : ""}${snippet}${needsTrailingBreak ? "\n" : ""}`;
  const text = `${before}${insertion}${after}`;
  return { text, caret: before.length + insertion.length };
}

export function MessageInput({
  chatId,
  replyTo,
  messages,
  lockedBy,
}: {
  chatId: number;
  replyTo: Message | null;
  messages: Message[];
  lockedBy: { operator_id: number; operator_name: string; remaining_ms: number } | null;
}): JSX.Element {
  const draft = useCRMStore((state) => state.draft);
  const setDraft = useCRMStore((state) => state.setDraft);
  const sendMessage = useCRMStore((state) => state.sendMessage);
  const uploadFile = useCRMStore((state) => state.uploadFile);
  const setReplyTarget = useCRMStore((state) => state.setReplyTarget);
  const beginTyping = useCRMStore((state) => state.beginTyping);
  const endTyping = useCRMStore((state) => state.endTyping);
  const jumpToMessage = useCRMStore((state) => state.jumpToMessage);
  const sending = useCRMStore((state) => state.sending);

  /* CONTRACTS_V3 §2/§3.1 — edit mode. Only the id itself comes from the store;
     the editable text lives in `editingDraft`, and that keeps plain drafting and
     editing completely independent (switching into edit mode never eats a draft). */
  const editingMessageId = useCRMStore((state) => state.editingMessageId);
  const editingDraft = useCRMStore((state) => state.editingDraft);
  const cancelEditing = useCRMStore((state) => state.cancelEditing);
  const saveEdit = useCRMStore((state) => state.saveEdit);

  /* CONTRACTS_V5 §5 — the canned replies come from the store (the backend table),
     not from a constant in this file (§8.9). `selectActiveQuickReplies` keeps a
     stable identity for an empty list, so this never loops `useSyncExternalStore`. */
  const quickReplies = useCRMStore(selectActiveQuickReplies);
  const loadQuickReplies = useCRMStore((state) => state.loadQuickReplies);
  const quickRepliesLoading = useCRMStore((state) => state.quickRepliesLoading);
  const quickRepliesError = useCRMStore((state) => state.quickRepliesError);

  const isEditing = editingMessageId !== null;

  /** Local mirror of `editingDraft`: Zustand has no per-action "setDraft". */
  const [editText, setEditText] = useState(editingDraft);

  /* Re-seed the local textarea only when the *target* message changes — not on
     every `editingDraft` write (that would fight the operator's caret). */
  const seededEditRef = useRef<number | null>(null);
  useEffect(() => {
    if (editingMessageId === null) {
      seededEditRef.current = null;
      setEditText("");
      return;
    }
    if (seededEditRef.current === editingMessageId) return;
    seededEditRef.current = editingMessageId;
    setEditText(editingDraft);
    /* focus the composer so Enter/Esc work right away */
    const element = textareaRef.current;
    if (element) {
      element.focus();
      const end = element.value.length;
      element.setSelectionRange(end, end);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingMessageId]);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [showQuick, setShowQuick] = useState(false);
  /** §5 — the CRUD editor dialog (its open state belongs to this composer). */
  const [editorOpen, setEditorOpen] = useState(false);
  /* CONTRACTS_V2 §3.1 — files staged for the next send. */
  const [files, setFiles] = useState<File[]>([]);

  /* §5.2 — load the list once per session; the store keeps it warm and the §5.3
     WS frame keeps it fresh, so a chat switch costs nothing. */
  useEffect(() => {
    void loadQuickReplies();
  }, [loadQuickReplies]);

  const isLocked = lockedBy !== null && lockedBy.remaining_ms > 0;
  /* The collision lock protects the *send* path only: editing an existing message
     must stay possible while another operator holds the chat, otherwise a typo
     would be impossible to fix during a busy shift. */
  const composerLocked = isLocked && !isEditing;

  /* autofocus once per chat switch — NOT on every lock/countdown change, because
     calling focus() mid-typing resets the caret and drops keystrokes. */
  const focusChatRef = useRef<number | null>(null);
  useEffect(() => {
    if (focusChatRef.current === chatId) return;
    focusChatRef.current = chatId;
    if (!isLocked) textareaRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId]);

  /* auto-grow */
  const bodyText = isEditing ? editText : draft;
  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(element.scrollHeight, 140)}px`;
  }, [bodyText]);

  /**
   * The collision lock is claimed on the first keystroke of a draft, never on focus:
   * merely opening a chat must not reserve it, otherwise browsing the list would block
   * every other operator. It is claimed exactly once per draft (guarded by a ref rather
   * than by state) so typing does not re-enter the store on every character — a
   * controlled textarea that re-renders on each keystroke loses characters and the caret.
   */
  const lockClaimedRef = useRef(false);

  const handleChange = useCallback(
    (event: ChangeEvent<HTMLTextAreaElement>) => {
      const value = event.target.value.slice(0, MAX_LENGTH);
      /* Edit mode writes its own field: it must not touch the chat draft and must
         not claim the collision lock (no message is being composed). */
      if (isEditing) {
        setEditText(value);
        return;
      }
      setDraft(value);
      if (!lockClaimedRef.current && !isLocked && value.length > 0) {
        lockClaimedRef.current = true;
        beginTyping(chatId);
      }
      if (value.length === 0) lockClaimedRef.current = false;
    },
    [beginTyping, chatId, isEditing, isLocked, setDraft],
  );

  /* a chat switch starts a fresh draft, so the claim must reset with it */
  useEffect(() => {
    lockClaimedRef.current = false;
  }, [chatId]);

  /* staged files belong to the chat they were picked in */
  useEffect(() => {
    setFiles([]);
  }, [chatId]);

  /* edit mode is scoped to one chat — leaving it cancels the edit (also handled
     by the store's `selectChat`, this covers a parent-driven `chatId` change). */
  useEffect(() => {
    cancelEditing();
  }, [chatId, cancelEditing]);

  const handleBlur = useCallback(() => {
    /* Releasing on blur would fight with clicking Send; the 15 s TTL is the real guard. */
  }, []);

  /* CONTRACTS_V2 §3.1 — FilePicker hands over freshly picked files. */
  const handlePick = useCallback(
    (picked: File[]) => {
      if (picked.length === 0) return;
      setFiles((current) => {
        // De-duplicate by name+size+mtime so dropping the same file twice is a no-op.
        const seen = new Set(current.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
        const added = picked.filter((file) => !seen.has(`${file.name}:${file.size}:${file.lastModified}`));
        return added.length === 0 ? current : [...current, ...added];
      });
      if (!lockClaimedRef.current && !isLocked) {
        lockClaimedRef.current = true;
        beginTyping(chatId);
      }
    },
    [beginTyping, chatId, isLocked],
  );

  const removeFile = useCallback((index: number) => {
    setFiles((current) => current.filter((_, position) => position !== index));
  }, []);

  const submit = useCallback(async () => {
    if (isLocked || sending) return;
    const hasFiles = files.length > 0;
    const hasText = draft.trim().length > 0;
    if (!hasFiles && !hasText) return;
    /* Refuse instead of letting the store cut the caption to 1024 characters. */
    if (hasFiles && draft.length > MAX_MEDIA_CAPTION_LENGTH) return;

    endTyping(chatId);
    if (hasFiles) {
      const ok = await uploadFile(chatId, files, draft, replyTo?.id ?? null);
      if (ok) {
        setFiles([]);
        lockClaimedRef.current = false;
        textareaRef.current?.focus();
      }
      return;
    }
    await sendMessage();
    textareaRef.current?.focus();
  }, [chatId, draft, endTyping, files, isLocked, replyTo, sendMessage, sending, uploadFile]);

  /** CONTRACTS_V3 §2.3 — Enter in edit mode saves through `PATCH /api/messages/{id}`. */
  const submitEdit = useCallback(async () => {
    if (!isEditing || sending) return;
    const text = editText.slice(0, MAX_LENGTH).trim();
    if (!text) return;
    await saveEdit(text);
    textareaRef.current?.focus();
  }, [editText, isEditing, saveEdit, sending]);

  /**
   * CONTRACTS_V5 §5.4 — insert a quick reply **at the caret**, not at the end.
   *
   * The textarea is controlled, so its DOM value is whatever React rendered last;
   * `selectionStart/End` are still accurate between renders. The caret is restored
   * after React commits the new value (a `requestAnimationFrame`), otherwise the
   * browser parks it at the end of the text and the operator keeps typing in the
   * wrong place.
   */
  const insertQuickReply = useCallback(
    (reply: QuickReply) => {
      const element = textareaRef.current;
      const body = reply.body;
      if (!body) return;

      /* In edit mode the quote is frozen by `PATCH`, so a canned reply targets the
         draft — switching modes silently would lose the operator's edit. */
      const current = isEditing ? editText : draft;
      const start = element?.selectionStart ?? current.length;
      const end = element?.selectionEnd ?? current.length;
      const { text, caret } = insertAtCaret(current, start, end, body);
      const limited = text.slice(0, MAX_LENGTH);
      const safeCaret = Math.min(caret, limited.length);

      if (isEditing) setEditText(limited);
      else setDraft(limited);

      setShowQuick(false);
      if (!isEditing && !lockClaimedRef.current && !isLocked) {
        lockClaimedRef.current = true;
        beginTyping(chatId);
      }
      window.requestAnimationFrame(() => {
        const node = textareaRef.current;
        if (!node) return;
        node.focus();
        node.setSelectionRange(safeCaret, safeCaret);
      });
    },
    [beginTyping, chatId, draft, editText, isEditing, isLocked, setDraft],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (isEditing) {
      /* Edit mode owns Enter/Esc: no reply quoting, no "↑ recalls last message". */
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        void submitEdit();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        cancelEditing();
        return;
      }
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
      return;
    }
    if (event.key === "Escape") {
      if (replyTo) {
        event.preventDefault();
        setReplyTarget(null);
      }
      return;
    }
    if (event.key === "ArrowUp" && draft === "" && !replyTo) {
      const lastIncoming = [...messages].reverse().find((message) => !message.is_outgoing);
      if (lastIncoming) {
        event.preventDefault();
        setReplyTarget(lastIncoming.id);
      }
    }
  };

  /**
   * A send with attachments becomes a media caption, which Telegram caps far
   * below the plain-text limit. The counter and the "blocked" state advertise that
   * cap up front instead of letting the store silently truncate the text on send.
   */
  const counterLimit = files.length > 0 ? MAX_MEDIA_CAPTION_LENGTH : MAX_LENGTH;
  const overLimit = bodyText.length > counterLimit;
  const shownRemaining = counterLimit - bodyText.length;
  const canSend = isEditing
    ? editText.trim().length > 0 && !sending && !overLimit
    : (draft.trim().length > 0 || files.length > 0) && !isLocked && !sending && !overLimit;

  const blockedMessage = isEditing
    ? editText.trim().length > 0
      ? "Сохранить изменения (Enter) · Esc — отмена"
      : "Введите текст сообщения"
    : isLocked
      ? "Заблокировано другим оператором"
      : overLimit
        ? `Подпись к файлу не должна превышать ${MAX_MEDIA_CAPTION_LENGTH} символов`
        : files.length > 0
          ? "Отправить файлы (Enter)"
          : "Отправить (Enter)";

  /* The message being edited — only for the header line of the banner. */
  const editingMessage = useMemo(
    () => (editingMessageId === null ? null : (messages.find((item) => item.id === editingMessageId) ?? null)),
    [editingMessageId, messages],
  );
  return (
    <div className="shrink-0 border-t border-tg-border bg-tg-panel">
      {/* CONTRACTS_V3 §3.1 — edit banner (Enter = сохранить, Esc = отмена) */}
      {isEditing && (
        <div className="flex animate-fade-in items-center gap-2 border-b border-tg-accent/30 bg-tg-accent/10 px-3 py-1.5 text-2xs text-tg-accentHover">
          <Pencil size={13} className="shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="font-semibold">Редактирование сообщения</span>
            <span className="ml-1.5 text-tg-textMuted">
              {editingMessage ? `· #${editingMessage.id}` : ""} · Enter — сохранить, Esc — отмена
            </span>
          </span>
          <button
            type="button"
            onClick={cancelEditing}
            className="shrink-0 rounded p-1 text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text"
            aria-label="Отменить редактирование"
            title="Отменить редактирование (Esc)"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {/* reply preview — hidden while editing: a PATCH cannot change the quote */}
      {replyTo && !isEditing && (
        <div className="flex items-center gap-2 border-b border-tg-border px-3 py-1.5">
          <CornerUpLeft size={14} className="shrink-0 text-tg-accentHover" />
          <button
            type="button"
            onClick={() => jumpToMessage(replyTo.id)}
            className="flex min-w-0 flex-1 flex-col border-l-2 border-tg-accent bg-tg-elevated/60 px-2 py-1 text-left"
            title="Перейти к исходному сообщению"
          >
            <span className="flex items-center gap-1 text-2xs font-semibold text-tg-accentHover">
              Reply to {replyTo.is_outgoing ? "your message" : replyTo.sender_name || "unknown sender"}
              {replyTo.is_deleted_in_tg && (
                <span className="inline-flex items-center gap-0.5 text-tg-danger">
                  <Trash2 size={8} /> deleted in TG
                </span>
              )}
            </span>
            <span className="line-clamp-1 text-2xs text-tg-textMuted">
              {/* `text` is `str` on the backend but a row ingested before V2 can carry
                  `NULL`, so the quote preview coerces it instead of throwing inside
                  `previewText` and blanking the whole composer. */}
              {replyTo.text
                ? previewText(replyTo.text, 120)
                : replyTo.attachments?.length
                  ? `[вложение ×${replyTo.attachments.length}]`
                  : "[empty]"}
            </span>
          </button>
          <button
            type="button"
            onClick={() => setReplyTarget(null)}
            className="shrink-0 rounded p-1 text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text"
            aria-label="Отменить ответ"
            title="Отменить ответ (Esc)"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {/* CONTRACTS_V2 §3.1 — staged file previews (not applicable to an edit: PATCH
          changes text only, so staging files would silently drop them) */}
      {files.length > 0 && !isEditing && (
        <div className="animate-fade-in border-b border-tg-border bg-tg-panelAlt px-3 py-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-2xs uppercase tracking-wider text-tg-textFaint">
              Вложения · {files.length}
            </span>
            <button
              type="button"
              onClick={() => setFiles([])}
              className="inline-flex items-center gap-1 text-2xs text-tg-textFaint transition hover:text-tg-danger"
              aria-label="Очистить все вложения"
              title="Очистить все вложения"
            >
              <Trash2 size={11} /> Очистить
            </button>
          </div>
          <ul className="flex max-h-[104px] flex-wrap gap-1.5 overflow-y-auto scroll-area">
            {files.map((file, index) => (
              <li
                key={`${file.name}-${file.size}-${file.lastModified}-${index}`}
                className="flex max-w-[240px] items-center gap-1.5 rounded-md border border-tg-border bg-tg-elevated px-2 py-1"
                title={file.name}
              >
                <FileText size={12} className="shrink-0 text-tg-accentHover" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-2xs font-medium text-tg-text">{file.name}</span>
                  <span className="block text-[10px] tabular text-tg-textFaint">{formatBytes(file.size)}</span>
                </span>
                <button
                  type="button"
                  onClick={() => removeFile(index)}
                  className="grid h-4 w-4 shrink-0 place-items-center rounded text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-danger"
                  aria-label={`Убрать ${file.name}`}
                  title="Убрать файл"
                >
                  <X size={11} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* CONTRACTS_V5 §5 — quick replies come from the store (backend table) */}
      {showQuick && (
        <div className="animate-fade-in border-b border-tg-border bg-tg-panelAlt px-3 py-2">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-2xs uppercase tracking-wider text-tg-textFaint">
              Быстрые ответы · {quickReplies.length}
            </span>
            <span className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => {
                  setShowQuick(false);
                  setEditorOpen(true);
                }}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-accentHover"
                aria-label="Редактировать быстрые ответы"
                title="Редактировать список быстрых ответов"
              >
                <Settings2 size={11} /> Изменить
              </button>
              <button
                type="button"
                onClick={() => setShowQuick(false)}
                className="rounded p-1 text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-text"
                aria-label="Закрыть быстрые ответы"
                title="Закрыть"
              >
                <X size={12} />
              </button>
            </span>
          </div>

          {quickRepliesError && quickReplies.length === 0 && (
            <div className="mb-1 flex items-start gap-1.5 rounded border border-tg-danger/30 bg-tg-danger/10 px-2 py-1 text-[10px] leading-4 text-tg-danger">
              <span className="flex-1">{quickRepliesError}</span>
              <button
                type="button"
                onClick={() => void loadQuickReplies({ force: true })}
                className="shrink-0 font-semibold underline hover:text-white"
                aria-label="Повторить загрузку быстрых ответов"
              >
                Повторить
              </button>
            </div>
          )}

          <div className="flex max-h-56 flex-col gap-1 overflow-y-auto scroll-area">
            {quickRepliesLoading && quickReplies.length === 0 ? (
              <p className="flex items-center justify-center gap-1.5 px-1 py-2 text-2xs text-tg-textFaint">
                <Loader2 size={11} className="animate-spin" /> Загрузка быстрых ответов…
              </p>
            ) : quickReplies.length === 0 ? (
              <p className="px-1 py-2 text-center text-2xs text-tg-textFaint">
                {quickRepliesError ? "Список недоступен." : "Пусто — создайте первый быстрый ответ."}
              </p>
            ) : (
              quickReplies.map((reply) => (
                <button
                  key={reply.id}
                  type="button"
                  onClick={() => insertQuickReply(reply)}
                  /* Full text must be readable: the body wraps to two lines instead of
                     being cut to a single ellipsised line, and the title acts as the
                     caption while the body stays the tooltip. */
                  title={reply.body}
                  aria-label={`Вставить быстрый ответ «${reply.title}»`}
                  className="group/qr flex w-full flex-col gap-0.5 rounded px-2 py-1 text-left transition hover:bg-tg-hover"
                >
                  <span className="flex items-center gap-1">
                    <span className="truncate text-2xs font-semibold text-tg-textMuted group-hover/qr:text-tg-text">
                      {reply.title || reply.shortcut}
                    </span>
                    {reply.shortcut && (
                      <span className="shrink-0 font-mono text-[9px] text-tg-textFaint">/{reply.shortcut}</span>
                    )}
                  </span>
                  <span className="line-clamp-2 whitespace-pre-wrap break-words text-[10px] leading-4 text-tg-textFaint group-hover/qr:text-tg-textMuted">
                    {reply.body}
                  </span>
                </button>
              ))
            )}
          </div>
        </div>
      )}

      {/* composer row */}
      <div className="flex items-end gap-2 px-3 py-2.5">
        <div className="relative">
          <button
            type="button"
            onClick={() => setShowQuick((open) => !open)}
            className={`grid h-9 w-9 place-items-center rounded-full transition ${
              showQuick ? "bg-tg-accent/20 text-tg-accentHover" : "text-tg-textMuted hover:bg-tg-hover hover:text-tg-text"
            }`}
            aria-label="Быстрые ответы"
            title="Быстрые ответы"
            aria-expanded={showQuick}
          >
            {/* §5.4 — the old Smile icon clashed with the emoji/reaction pickers
                elsewhere in the timeline; Zap reads as "canned reply". */}
            <Zap size={18} />
          </button>
        </div>

        {/* CONTRACTS_V2 §3.1 — attach files (button + drag&drop) */}
        <FilePicker onPick={handlePick} disabled={composerLocked || sending || isEditing} />

        <div
          className={`flex min-w-0 flex-1 items-end gap-2 rounded-2xl border px-3 py-1.5 transition-colors ${
            isEditing
              ? "border-tg-accent/60 bg-tg-elevated shadow-glow"
              : isLocked
                ? "border-tg-warning/50 bg-tg-warning/5"
                : draft.length > 0 || files.length > 0
                  ? "border-tg-accent/40 bg-tg-elevated"
                  : "border-transparent bg-tg-elevated"
          }`}
        >
          {isEditing && <Pencil size={13} className="mb-1.5 shrink-0 text-tg-accentHover" />}
          {isLocked && !isEditing && <Lock size={13} className="mb-1.5 shrink-0 text-tg-warning" />}
          <textarea
            ref={textareaRef}
            value={isEditing ? editText : draft}
            disabled={composerLocked}
            onChange={handleChange}
            onBlur={handleBlur}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder={
              isEditing
                ? "Изменить сообщение…  (Enter = сохранить, Esc = отмена)"
                : isLocked
                  ? `🔒 Locked — ${lockedBy?.operator_name ?? "another operator"} is replying…`
                  : files.length > 0
                    ? "Подпись к файлу…  (Enter = отправить)"
                    : "Write a message…  (Enter = send, Shift+Enter = newline, Esc = cancel reply)"
            }
            className="max-h-[140px] min-h-[22px] w-full resize-none bg-transparent py-1 text-[13px] leading-5 text-tg-text placeholder:text-tg-textFaint disabled:cursor-not-allowed disabled:text-tg-textFaint focus:outline-none"
            aria-label={isEditing ? "Текст сообщения (редактирование)" : "Текст сообщения"}
          />
          {overLimit && (
            <span className="mb-1.5 shrink-0 text-2xs tabular font-semibold text-tg-danger">
              {shownRemaining}
            </span>
          )}
          {!overLimit && bodyText.length > counterLimit * 0.85 && (
            <span className={`mb-1.5 shrink-0 text-2xs tabular ${shownRemaining < 200 ? "text-tg-danger" : "text-tg-textFaint"}`}>
              {shownRemaining}
            </span>
          )}
        </div>

        <button
          type="button"
          onClick={() => void (isEditing ? submitEdit() : submit())}
          disabled={!canSend || overLimit}
          className={`grid h-10 w-10 shrink-0 place-items-center rounded-full transition ${
            canSend
              ? "bg-tg-accent text-white hover:bg-tg-accentHover"
              : "cursor-not-allowed bg-tg-elevated text-tg-textFaint"
          }`}
          aria-label={isEditing ? "Сохранить изменения" : "Отправить сообщение"}
          title={blockedMessage}
        >
          {sending ? (
            <Loader2 size={17} className="animate-spin" />
          ) : isEditing ? (
            <Check size={18} />
          ) : (
            <Send size={17} />
          )}
        </button>
      </div>

      <div className="flex items-center justify-between px-4 pb-1.5 text-[10px] text-tg-textFaint">
        <span>
          {isEditing ? (
            <>
              Режим правки:{" "}
              <span className="text-tg-accentHover">
                {editingMessage ? `#${editingMessage.id}` : "сообщение"}
              </span>
              <span className="ml-2 text-tg-textMuted">Enter — сохранить · Esc — отмена</span>
            </>
          ) : (
            <>
              Reply mode:{" "}
              <span className={replyTo ? "text-tg-accentHover" : "text-tg-textMuted"}>
                {replyTo ? `quoting #${replyTo.id}` : "off (doubles-click / ↑ to quote)"}
              </span>
              {files.length > 0 && (
                <span className="ml-2 text-tg-accentHover">
                  · {files.length} файл(ов), {formatBytes(files.reduce((sum, file) => sum + file.size, 0))}
                </span>
              )}
            </>
          )}
        </span>
        <span className="flex items-center gap-3">
          <span className={overLimit ? "font-semibold text-tg-danger" : undefined}>
            Characters: {bodyText.length}
          </span>
          <span className="hidden sm:inline">
            {files.length > 0 && !isEditing ? `Подпись до ${MAX_MEDIA_CAPTION_LENGTH}` : `TG limit ${MAX_LENGTH}`}
          </span>
        </span>
      </div>

      {/* CONTRACTS_V5 §5.2 — CRUD editor, mounted next to the composer that owns it */}
      <QuickReplyEditor open={editorOpen} onClose={() => setEditorOpen(false)} />
    </div>
  );
}
