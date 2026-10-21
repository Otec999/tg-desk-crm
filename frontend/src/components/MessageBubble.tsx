import {
  AlertTriangle,
  BadgeCheck,
  Bot,
  Check,
  CheckCheck,
  Clock,
  Copy,
  CornerUpLeft,
  FileSearch,
  Forward,
  Loader2,
  Pencil,
  ShieldQuestion,
  Trash2,
  XCircle,
} from "lucide-react";
import { memo, useCallback, useMemo, type ReactNode } from "react";

import { extractTxIds, formatClock, formatClockSeconds } from "../lib/format";
import type { Attachment, Message } from "../lib/types";
import {
  EMPTY_ATTACHMENTS,
  EMPTY_REACTIONS,
  selectEditHistory,
  selectEditHistoryError,
  selectEditHistoryLoading,
  selectEditHistoryOpen,
  useCRMStore,
} from "../store/useCRMStore";
import EditHistory from "./EditHistory";
import { MediaBubble } from "./MediaBubble";
import { MessageDeleteAction } from "./MessageDeleteAction";
import { ReactionBar } from "./ReactionBar";

export interface MessageBubbleProps {
  message: Message;
  /** CRM id of the message this one replies to (resolved client-side). */
  quotedMessage: Message | null;
  isCurrentUserAuthor: boolean;
  highlighted: boolean;
  onReply: (message: Message) => void;
  onJumpTo: (messageId: number) => void;
  onSearchTx: (txId: string) => void;
  /** CONTRACTS_V2 §3.2 — place/replace my reaction on this message. */
  onReact?: (messageId: number, emoji: string) => void;
  /** CONTRACTS_V2 §3.2 — drop my reaction from this message. */
  onUnreact?: (messageId: number) => void;
  /** CONTRACTS_V2 §3.1 — open the full-screen viewer at `index`. */
  onOpenMedia?: (attachments: Attachment[], index: number) => void;
  /** CONTRACTS_V3 §3.1 — open the forward dialog for this message. */
  onForward?: (message: Message) => void;
  /** CONTRACTS_V3 §3.1 — enter edit mode with this message. */
  onEdit?: (message: Message) => void;
  /* CONTRACTS_V5 §1.3 — deletion deliberately needs **no** prop here: the action
     is the self-contained `MessageDeleteAction` child (it resolves the operator's
     right from the store), so `ChatWindow` owns nothing and this prop list stays
     stable for the memoised bubble. */
}

/** Stable no-op defaults: re-created callbacks would break `memo` on every parent render. */
const NOOP_REACT = (): void => undefined;
const NOOP_UNREACT = (): void => undefined;
const NOOP_OPEN_MEDIA = (): void => undefined;
const NOOP_FORWARD = (): void => undefined;
const NOOP_EDIT = (): void => undefined;

/* ------------------------- CONTRACTS_V3 §3.1 — per-message store bindings
 *
 * These live in a tiny leaf component rather than in `MessageBubble` itself:
 * the bubble is `memo`-ised and must not subscribe to store slices that change
 * for one message only (that would re-render the whole timeline).
 */

/** Badge rendered in the footer meta row: «(изменено)». */
function EditedBadge({ messageId }: { messageId: number }): JSX.Element | null {
  const open = useCRMStore(selectEditHistoryOpen(messageId));
  const edits = useCRMStore(selectEditHistory(messageId));
  const toggle = useCRMStore((state) => state.toggleEditHistory);
  const count = edits.length;

  return (
    <button
      type="button"
      onClick={() => toggle(messageId)}
      className={`rounded px-0.5 italic transition hover:text-tg-accentHover ${open ? "text-tg-accentHover" : ""}`}
      title={
        count > 0
          ? `Изменено из Telegram · правок: ${count}. Нажмите, чтобы показать историю`
          : "Сообщение было изменено"
      }
      aria-expanded={open}
      aria-label="Сообщение изменено"
    >
      (изменено)
    </button>
  );
}

/**
 * Edit-history attach point (CONTRACTS_V3 §3.1).
 *
 * Rendered when a history is already known **or** the block is expanded (so the
 * operator can open an empty/loading block and see why it is empty). It stays a
 * leaf so a history reload never touches the bubble's props.
 */
function MessageEditHistory({ messageId }: { messageId: number }): JSX.Element | null {
  const edits = useCRMStore(selectEditHistory(messageId));
  const open = useCRMStore(selectEditHistoryOpen(messageId));
  const loading = useCRMStore(selectEditHistoryLoading(messageId));
  const error = useCRMStore(selectEditHistoryError(messageId));
  const toggle = useCRMStore((state) => state.toggleEditHistory);

  if (edits.length === 0 && !open) return null;

  return (
    <EditHistory
      edits={edits}
      open={open}
      loading={loading}
      error={error}
      onToggle={() => toggle(messageId)}
    />
  );
}

function MessageBubbleBase({
  message,
  quotedMessage,
  isCurrentUserAuthor,
  highlighted,
  onReply,
  onJumpTo,
  onSearchTx,
  onReact = NOOP_REACT,
  onUnreact = NOOP_UNREACT,
  onOpenMedia = NOOP_OPEN_MEDIA,
  onForward = NOOP_FORWARD,
  onEdit = NOOP_EDIT,
}: MessageBubbleProps): JSX.Element {
  const outgoing = message.sender_type === "OPERATOR";
  const deleted = message.is_deleted_in_tg;
  const isBot = message.sender_type === "BOT";
  const isSystem = message.sender_type === "SYSTEM";

  const attachments = message.attachments ?? EMPTY_ATTACHMENTS;
  const reactions = message.reactions ?? EMPTY_REACTIONS;
  const hasAttachments = attachments.length > 0;

  const txIds = useMemo(() => extractTxIds(message.text), [message.text]);

  /* CONTRACTS_V5 §4.3 п.2 — `memo(ReactionBar)` only pays off while these two
     intents keep their identity across renders; inline arrows here made every
     bubble re-render its reaction bar on each parent pass. Declared before the
     SYSTEM-message early return so the hook order stays unconditional. */
  const messageId = message.id;
  const handleAddReaction = useCallback(
    (emoji: string): void => onReact(messageId, emoji),
    [messageId, onReact],
  );
  const handleRemoveReaction = useCallback((): void => onUnreact(messageId), [messageId, onUnreact]);

  if (isSystem) {
    return (
      <div className="my-1 flex justify-center">
        <span className="rounded-full bg-tg-elevated/80 px-2.5 py-1 text-2xs text-tg-textMuted">
          {message.text}
        </span>
      </div>
    );
  }

  const bubbleClass = outgoing
    ? "bubble-out rounded-xl rounded-br-sm"
    : `bubble-in rounded-xl rounded-bl-sm ${isBot ? "border border-tg-accent/25" : ""}`;

  const deletedClass = deleted ? "bubble-deleted" : "";
  const quotedDeleted = quotedMessage?.is_deleted_in_tg ?? false;
  /* Media carries its own padding: the picture must reach the bubble edges. */
  const bubblePadding = hasAttachments ? "px-1 pb-1.5 pt-1" : "px-2.5 py-1.5";
  /* A media message with an empty caption hides the (empty) text block entirely. */
  const showBody = !hasAttachments || message.text.trim().length > 0;

  return (
    <div
      id={`msg-${message.id}`}
      data-message-id={message.id}
      className={`group flex w-full gap-2 px-4 py-0.5 ${outgoing ? "justify-end" : "justify-start"} ${
        highlighted ? "flash-target rounded-lg" : ""
      }`}
      onDoubleClick={() => onReply(message)}
    >
      {!outgoing && <SenderAvatar message={message} />}

      <div className={`flex max-w-[min(680px,78%)] flex-col ${outgoing ? "items-end" : "items-start"}`}>
        {/* name row */}
        <div className="mb-0.5 flex items-center gap-1 pl-1 pr-1 text-2xs">
          {outgoing ? (
            <span className="text-tg-accentHover">You</span>
          ) : (
            <span className={isBot ? "font-semibold text-tg-accent" : "font-semibold text-tg-info"}>
              {message.sender_name}
            </span>
          )}
          {/* CONTRACTS_V2 §3.4 — bots get their own 🤖 badge and accent colour */}
          {isBot && (
            <span
              className="inline-flex items-center gap-0.5 rounded bg-tg-accent/15 px-1 text-[9px] font-bold uppercase text-tg-accent"
              title="Ответы на сообщения бота воркер отправит как обычный текст"
            >
              <span aria-hidden>🤖</span> bot
            </span>
          )}
          {message.sender_username && (
            <span className="truncate font-mono text-[9px] text-tg-textMuted">@{message.sender_username}</span>
          )}
          {message.sender_tg_id !== null && (
            <span className="font-mono text-[9px] text-tg-textFaint">#{message.sender_tg_id}</span>
          )}
        </div>

        {/* bubble */}
        <div className={`relative max-w-full ${bubblePadding} shadow-bubble ${bubbleClass} ${deletedClass}`}>
          {/* reply header */}
          {quotedMessage && (
            <button
              type="button"
              onClick={() => onJumpTo(quotedMessage.id)}
              className={`mb-1 flex w-full flex-col gap-0.5 rounded-md border-l-2 px-1.5 py-1 text-left transition ${
                outgoing ? "bg-black/15 hover:bg-black/25" : "bg-white/[0.04] hover:bg-white/[0.09]"
              } ${quotedDeleted ? "border-tg-danger/70" : "border-tg-accentHover"}`}
              title="Перейти к цитируемому сообщению"
            >
              <span
                className={`text-[10px] font-semibold ${
                  quotedDeleted ? "text-tg-danger" : outgoing ? "text-white/90" : "text-tg-accentHover"
                }`}
              >
                {quotedMessage.sender_type === "OPERATOR" ? "You" : quotedMessage.sender_name}
                {quotedDeleted && (
                  <span className="ml-1 inline-flex items-center gap-0.5 text-[9px] uppercase">
                    <Trash2 size={8} /> deleted
                  </span>
                )}
              </span>
              <span
                className={`line-clamp-2 text-[11px] ${
                  outgoing ? "text-white/75" : "text-tg-textMuted"
                } ${quotedDeleted ? "line-through decoration-tg-danger/50" : ""}`}
              >
                {quotedMessage.text || "[вложение]"}
              </span>
            </button>
          )}

          {/* CONTRACTS_V2 §3.1 — forwarded-from marker */}
          {message.forwarded_from && (
            <div
              className={`mb-1 flex items-center gap-1 text-[10px] font-medium ${
                outgoing ? "text-white/75" : "text-tg-textMuted"
              }`}
              title={`Переслано от ${message.forwarded_from}`}
            >
              <Forward size={10} className="shrink-0" />
              <span className="truncate">Переслано от {message.forwarded_from}</span>
            </div>
          )}

          {/* deleted banner — CONTRACTS_V5 §1.2 also reports *who* deleted it, so
              the operator is not told "by sender" after deleting it from the CRM. */}
          {deleted && (
            <div className="mb-1 flex items-start gap-1.5 rounded-md border border-tg-border bg-tg-elevated px-1.5 py-1 text-[10px] font-semibold text-tg-textMuted">
              <Trash2 size={11} className="mt-[1px] shrink-0" />
              <span>
                {deletedLabel(message.deleted_source)}{" "}
                <span className="tabular text-tg-textFaint">{formatClockSeconds(message.deleted_at)}</span>
              </span>
            </div>
          )}

          {/* CONTRACTS_V2 §3.1 — media (photo / file / voice) */}
          {hasAttachments && (
            <div className={deleted ? "opacity-60 grayscale" : ""}>
              {/* `MediaBubble` reports the *global* index inside the message so the
                  lightbox can navigate across every attachment, photos included. */}
              <MediaBubble
                attachments={attachments}
                isOwn={outgoing}
                onOpen={(_attachment: Attachment, index: number) => onOpenMedia(attachments, index)}
              />
            </div>
          )}

          {/* body */}
          {showBody && (
            <div
              className={`whitespace-pre-wrap break-words text-[13px] leading-[1.35] ${hasAttachments ? "px-1.5" : ""} ${
                deleted ? "text-tg-textMuted line-through decoration-tg-textFaint/70 decoration-1" : ""
              }`}
            >
              <RichText text={message.text} onSearchTx={onSearchTx} outgoing={outgoing} />
            </div>
          )}

          {/* footer meta */}
          <div
            className={`mt-0.5 flex items-center justify-end gap-1 px-1.5 text-[10px] ${
              outgoing ? "text-white/70" : "text-tg-textFaint"
            }`}
          >
            {message.sla_incident_id !== null && (
              <span className="mr-auto inline-flex items-center gap-0.5 rounded bg-black/20 px-1 text-[9px] font-semibold text-tg-success">
                <Check size={8} /> SLA
              </span>
            )}
            {message.edited_at && <EditedBadge messageId={message.id} />}
            <span className="tabular">{formatClock(message.created_at)}</span>
            {outgoing && <DeliveryTicks status={message.delivery_status} />}
          </div>

          {/* internal operator attribution — visible only inside the CRM */}
          {outgoing && (
            <div className="mt-1 flex items-center gap-1 border-t border-white/15 px-1.5 pt-1 text-[10px] font-medium text-white/75">
              <BadgeCheck size={10} />
              Sent by: <span className="font-semibold">{message.sender_name || "operator"}</span>
              {isCurrentUserAuthor && <span className="text-white/50">(you)</span>}
            </div>
          )}
        </div>

        {/* CONTRACTS_V2 §3.2 — reaction bar (always rendered for live messages) */}
        {!deleted && (
          <ReactionBar
            messageId={message.id}
            chatId={message.chat_id}
            reactions={reactions}
            onAdd={handleAddReaction}
            onRemove={handleRemoveReaction}
            isOwnMessage={outgoing}
            align={outgoing ? "right" : "left"}
          />
        )}

        {/* hover actions */}
        <div className="mt-0.5 flex h-5 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <ActionButton label="Ответить" onClick={() => onReply(message)}>
            <CornerUpLeft size={11} />
          </ActionButton>
          <ActionButton
            label="Скопировать текст"
            onClick={() => void navigator.clipboard?.writeText(message.text).catch(() => undefined)}
          >
            <Copy size={11} />
          </ActionButton>
          {txIds[0] && (
            <ActionButton label={`Найти ${txIds[0]}`} onClick={() => onSearchTx(txIds[0])}>
              <FileSearch size={11} />
            </ActionButton>
          )}
          {/* CONTRACTS_V3 §3.1 — forward / edit. A message deleted in Telegram is
              frozen on the server (§5.5: PATCH answers 409) and cannot be copied,
              so both actions disappear for it. */}
          {!deleted && (
            <ActionButton label="Переслать" onClick={() => onForward(message)}>
              <Forward size={11} />
            </ActionButton>
          )}
          {!deleted && (
            <ActionButton label="Изменить" onClick={() => onEdit(message)}>
              <Pencil size={11} />
            </ActionButton>
          )}
          {/* CONTRACTS_V5 §1.3 — outbound deletion. The action reads the operator's
              right from the store itself and renders nothing when the message is
              already deleted (the server would answer 409) or the right is missing
              (403), so no `403` is reachable through the UI. */}
          <MessageDeleteAction message={message} />
        </div>

        {/* CONTRACTS_V3 §3.1 — «Изменено из Telegram» block under the bubble */}
        <MessageEditHistory messageId={message.id} />

        {txIds.length > 0 && (
          <div className="mt-0.5 flex flex-wrap items-center gap-1">
            {txIds.slice(0, 4).map((txId) => (
              <button
                key={txId}
                type="button"
                onClick={() => onSearchTx(txId)}
                className="inline-flex items-center gap-1 rounded-md border border-tg-accent/30 bg-tg-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-tg-accentHover transition hover:border-tg-accent/70 hover:bg-tg-accent/25 hover:text-white"
                title={`Искать ${txId} в обеих админ-панелях`}
              >
                <FileSearch size={9} />
                {txId}
              </button>
            ))}
          </div>
        )}
      </div>

      {outgoing && (
        <div
          className="mt-6 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-tg-accent/25 text-[10px] font-bold text-tg-accentHover opacity-0 transition-opacity group-hover:opacity-100"
          aria-hidden
        >
          {isCurrentUserAuthor ? "ME" : initialsof(message.sender_name)}
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- helpers */

/**
 * First two letters of a sender name, uppercased.
 *
 * `sender_name` is `NOT NULL` in the current schema, but rows ingested by older
 * builds can still return `null` over the wire; a single such row must degrade to a
 * neutral placeholder rather than throw inside the render and blank the timeline.
 */
function initialsof(name: string | null | undefined): string {
  const trimmed = (name ?? "").trim();
  return trimmed ? trimmed.slice(0, 2).toUpperCase() : "??";
}

/**
 * Banner wording per `deleted_source` (CONTRACTS_V5 §1.2).
 *
 * `CRM` — deleted by us from this console; `TELEGRAM` — deleted by the sender in
 * Telegram (the pre-V5 behaviour); unknown — a row read before the migration, so
 * the copy stays neutral instead of guessing the origin.
 */
function deletedLabel(source: Message["deleted_source"]): string {
  if (source === "CRM") return "Удалено оператором";
  if (source === "TELEGRAM") return "Удалено в Telegram";
  return "Deleted in Telegram by sender at";
}

function SenderAvatar({ message }: { message: Message }): JSX.Element {
  const isBot = message.sender_type === "BOT";
  return (
    <div
      className={`mt-4 grid h-7 w-7 shrink-0 place-items-center rounded-full text-[10px] font-bold ${
        isBot ? "bg-tg-accent/25 text-tg-accent" : "bg-tg-info/25 text-tg-info"
      }`}
      aria-hidden
    >
      {isBot ? <span title="Бот">🤖</span> : initialsof(message.sender_name)}
    </div>
  );
}

function ActionButton({
  label,
  title,
  onClick,
  children,
  tone = "default",
}: {
  label: string;
  /** Optional shorter tooltip when the accessible name is longer than the tip. */
  title?: string;
  onClick: () => void;
  children: ReactNode;
  /** `danger` paints a destructive action red without changing its footprint. */
  tone?: "default" | "danger";
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title ?? label}
      aria-label={label}
      className={`grid h-5 w-5 place-items-center rounded text-tg-textMuted transition hover:bg-tg-elevated ${
        tone === "danger" ? "hover:text-tg-danger" : "hover:text-tg-accentHover"
      }`}
    >
      {children}
    </button>
  );
}

function DeliveryTicks({ status }: { status: Message["delivery_status"] }): JSX.Element {
  if (status === "pending") return <Clock size={11} className="text-white/70" aria-label="Отправляется" />;
  if (status === "failed") return <XCircle size={11} className="text-tg-warning" aria-label="Ошибка доставки" />;
  return <CheckCheck size={12} className="text-white/85" aria-label="Доставлено" />;
}

/**
 * Renders message text with:
 * - `code` / monospace transaction ids as clickable chips (click-to-search),
 * - phone numbers as tooltip-only text.
 * Deliberately XSS-safe: no `dangerouslySetInnerHTML` anywhere.
 */
const RICH_REGEX = new RegExp(
  [
    // <code>...</code> or `...`
    "<code>([^<]+)</code>|`([^`]+)`|",
    // TX ids
    String.raw`\b((?:TX[- ]?\d{2,6}(?:-[0-9A-Z]{2,6}){0,2}))\b|`,
    // phone numbers
    String.raw`(\+?\d[\d()\s-]{7,}\d)`,
  ].join(""),
  "gi",
);

function RichText({
  text,
  onSearchTx,
  outgoing,
}: {
  text: string;
  onSearchTx: (txId: string) => void;
  outgoing: boolean;
}): JSX.Element {
  if (!text) return <span className="italic opacity-60">[media]</span>;

  const nodes: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(RICH_REGEX)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(text.slice(cursor, index));

    const [full, codeTag, codeTick, txToken] = match;
    if (codeTag !== undefined || codeTick !== undefined) {
      const codeText = (codeTag ?? codeTick) ?? "";
      const txInside = extractTxIds(codeText)[0];
      nodes.push(
        <button
          key={`code-${key++}`}
          type="button"
          onClick={() => txInside && onSearchTx(txInside)}
          className={`mx-0.5 inline-flex items-center gap-1 rounded border px-1 font-mono text-[0.88em] ${
            outgoing
              ? "border-white/35 bg-black/20 text-white hover:bg-black/35"
              : "border-tg-accent/40 bg-tg-accent/10 text-tg-accentHover hover:bg-tg-accent/25"
          }`}
          title={txInside ? `Найти ${txInside}` : "Скопировать"}
        >
          {codeText}
        </button>,
      );
    } else if (txToken !== undefined) {
      const canonical = txToken.toUpperCase().replace(/^TX /, "TX-");
      nodes.push(
        <button
          key={`tx-${key++}`}
          type="button"
          onClick={() => onSearchTx(canonical)}
          className={`mx-0.5 rounded border px-1 font-mono text-[0.88em] font-semibold transition ${
            outgoing
              ? "border-white/40 bg-black/20 text-white hover:bg-black/35"
              : "border-tg-accent/40 bg-tg-accent/10 text-tg-accentHover hover:bg-tg-accent/25 hover:text-white"
          }`}
          title={`Найти ${canonical} в админ-панелях 1 и 2`}
        >
          {txToken}
        </button>,
      );
    } else {
      nodes.push(
        <span key={`phone-${key++}`} className="tabular">
          {full}
        </span>,
      );
    }
    cursor = index + full.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));

  return <>{nodes}</>;
}

export const MessageBubble = memo(MessageBubbleBase);

/** Compact "is replying" strip used above the composer. */
export function TypingLockBanner({
  operatorName,
  remainingMs,
  onOverride,
}: {
  operatorName: string;
  remainingMs: number;
  onOverride?: () => void;
}): JSX.Element {
  const seconds = Math.max(0, Math.ceil(remainingMs / 1000));
  return (
    <div className="flex items-center gap-2 border-b border-tg-warning/25 bg-tg-warning/10 px-4 py-1.5 text-2xs text-tg-warning">
      <AlertTriangle size={12} className="shrink-0" />
      <span className="font-semibold">⚠️ {operatorName} is currently replying…</span>
      <span className="tabular text-tg-warning/70">input locked ({seconds}s)</span>
      {onOverride && (
        <button type="button" onClick={onOverride} className="ml-auto underline hover:text-white">
          take over
        </button>
      )}
    </div>
  );
}

/** Unused icon re-exports kept out of the bundle by tree shaking. */
export const __icons = { ShieldQuestion, Loader2, Bot };
