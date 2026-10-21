/**
 * Global CRM store (Zustand + immer-free immutable updates).
 *
 * Responsibilities
 * - Session lifecycle (login, token persistence, mock fallback).
 * - Chat list, message timelines, unread counters, assignment.
 * - SLA countdowns: a single 1s ticker interpolates every chat badge locally so
 *   the UI never depends on network cadence; `SLA_UPDATE` events re-anchor it.
 * - Collision lock (`TYPING_LOCK`) state per chat.
 * - Dual admin transaction lookup results + audit trail.
 * - CONTRACTS_V2 §3.1/§3.2/§4: media attachments, reactions (optimistic with
 *   rollback) and the `MESSAGE_MEDIA` / `MESSAGE_REACTION*` WebSocket frames.
 */

import { create } from "zustand";

import { ApiError, api } from "../lib/api";
import { secondsUntil, uuid } from "../lib/format";
import {
  MOCK_CURRENT_OPERATOR,
  MOCK_OPERATORS,
  MOCK_SLA_STATS,
  createMockDataset,
  formatTxForReply,
  mockLookupTx,
} from "../lib/mockData";
import type {
  Attachment,
  ChatFilter,
  ChatListItem,
  ConnectionState,
  Message,
  MessageEdit,
  MessageKind,
  Operator,
  PersonalSlaStats,
  QuickReply,
  QuickReplyCreatePayload,
  QuickReplyUpdatePayload,
  ReactionSummary,
  SearchHit,
  SearchResponse,
  SearchSenderFilter,
  ShiftStatus,
  TxAdminPanel,
  TxLookupResponse,
  UiChatItem,
  WsChatAssignedData,
  WsEnvelope,
  WsMessageDeletedData,
  WsMessageEditedData,
  WsMessageForwardedData,
  WsMessageMediaData,
  WsMessageReactionData,
  WsMessageStatusData,
  WsNewMessageData,
  WsQuickRepliesUpdatedData,
  WsShiftChangedData,
  WsSlaResolvedData,
  WsSlaUpdateData,
  WsTypingLockData,
  WsTypingUnlockData,
} from "../lib/types";
import { MESSAGE_KINDS } from "../lib/types";
import {
  QUICK_REPLY_BODY_MAX_LENGTH,
  QUICK_REPLY_CATEGORY_MAX_LENGTH,
  QUICK_REPLY_DEFAULT_CATEGORY,
  QUICK_REPLY_SHORTCUT_MAX_LENGTH,
  QUICK_REPLY_SHORTCUT_PATTERN,
  QUICK_REPLY_TITLE_MAX_LENGTH,
} from "../lib/types";
import type { WsClient } from "../lib/ws";
import { WsClient as WsClientImpl } from "../lib/ws";

const TOKEN_KEY = "tgdesk.token";
const OPERATOR_KEY = "tgdesk.operator";
const FORCED_MOCK = import.meta.env.VITE_MOCK_MODE === "true";

/** Mirrors the server-side typing lock TTL (CONTRACTS.md §5). */
const TYPING_LOCK_TTL_SECONDS = 15;

/** Telegram caption limit for a media message (CONTRACTS_V2 §3.1 → §2.1 `caption`). */
const MAX_MEDIA_CAPTION_LENGTH = 1024;

/** Telegram plain-text limit — an edit cannot exceed what `edit_message` accepts. */
const MAX_EDIT_LENGTH = 4096;

/* ------------------------------------------------ CONTRACTS_V5 §5 — helpers */

/** Optimistic quick-reply ids are negative, mirroring the optimistic message convention. */
let optimisticQuickReplySeq = -1;

function nextOptimisticQuickReplyId(): number {
  const id = optimisticQuickReplySeq;
  optimisticQuickReplySeq -= 1;
  return id;
}

/** Server ordering (§5.1 `INDEX (is_active, sort_order)`): sort_order, then id. */
function sortQuickReplies(items: readonly QuickReply[]): QuickReply[] {
  return [...items].sort((left, right) => {
    if (left.sort_order !== right.sort_order) return left.sort_order - right.sort_order;
    return left.id - right.id;
  });
}

/** Deep-ish equality for two reply lists, used to skip needless store writes. */
function sameQuickReplies(left: readonly QuickReply[], right: readonly QuickReply[]): boolean {
  if (left === right) return true;
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) {
    const a = left[index];
    const b = right[index];
    if (
      a.id !== b.id ||
      a.shortcut !== b.shortcut ||
      a.title !== b.title ||
      a.body !== b.body ||
      a.category !== b.category ||
      a.sort_order !== b.sort_order ||
      a.is_active !== b.is_active ||
      a.created_by !== b.created_by ||
      a.created_at !== b.created_at ||
      a.updated_at !== b.updated_at
    ) {
      return false;
    }
  }
  return true;
}

/** Coerce an untrusted `QUICK_REPLIES_UPDATED` frame into a clean reply list. */
function asQuickReplies(input: unknown): QuickReply[] | null {
  if (!Array.isArray(input)) return null;
  const items: QuickReply[] = [];
  for (const raw of input) {
    if (!raw || typeof raw !== "object") continue;
    const source = raw as Record<string, unknown>;
    const id = Number(source.id);
    if (!Number.isFinite(id)) continue;
    const body = typeof source.body === "string" ? source.body : "";
    if (!body.trim()) continue;
    const sortOrder = Number(source.sort_order);
    const category = typeof source.category === "string" && source.category.trim() ? source.category.trim() : "";
    items.push({
      id,
      shortcut: typeof source.shortcut === "string" ? source.shortcut : "",
      title: typeof source.title === "string" ? source.title : "",
      body,
      category: category || QUICK_REPLY_DEFAULT_CATEGORY,
      sort_order: Number.isFinite(sortOrder) ? sortOrder : 0,
      is_active: source.is_active !== false,
      created_by: typeof source.created_by === "string" ? source.created_by : null,
      created_at: typeof source.created_at === "string" ? source.created_at : "",
      updated_at: typeof source.updated_at === "string" ? source.updated_at : "",
    });
  }
  return sortQuickReplies(items);
}

/** Validation verdict for one reply. `null` means "valid". */
type QuickReplyValidationError = string | null;

/**
 * Client-side mirror of the backend normalisers (`app/schemas.py`, §5.2).
 *
 * Checking locally gives an instant, field-precise message instead of a generic
 * `422` round trip, but the backend stays authoritative: a body that is only
 * whitespace is rejected here for the same reason the DB `CHECK` rejects it.
 */
function validateQuickReplyFields(
  fields: { shortcut: string; title: string; body: string; category: string },
  siblings: readonly QuickReply[],
  selfId: number | null,
): QuickReplyValidationError {
  const shortcut = fields.shortcut.trim().toLowerCase();
  const title = fields.title.trim();
  const category = fields.category.trim();

  if (!shortcut) return "Короткий код не может быть пустым.";
  if (shortcut.length > QUICK_REPLY_SHORTCUT_MAX_LENGTH) {
    return `Короткий код длиннее ${QUICK_REPLY_SHORTCUT_MAX_LENGTH} символов.`;
  }
  if (!QUICK_REPLY_SHORTCUT_PATTERN.test(shortcut)) {
    return "Короткий код: только латинские буквы, цифры, «-» и «_».";
  }
  if (!title) return "Подпись не может быть пустой.";
  if (title.length > QUICK_REPLY_TITLE_MAX_LENGTH) {
    return `Подпись длиннее ${QUICK_REPLY_TITLE_MAX_LENGTH} символов.`;
  }
  if (!fields.body.trim()) return "Текст быстрого ответа не может быть пустым.";
  if (fields.body.length > QUICK_REPLY_BODY_MAX_LENGTH) {
    return `Текст длиннее ${QUICK_REPLY_BODY_MAX_LENGTH} символов — Telegram не примет такое сообщение.`;
  }
  if (category.length > QUICK_REPLY_CATEGORY_MAX_LENGTH) {
    return `Категория длиннее ${QUICK_REPLY_CATEGORY_MAX_LENGTH} символов.`;
  }
  const collision = siblings.find(
    (item) => item.id !== selfId && item.shortcut.trim().toLowerCase() === shortcut,
  );
  if (collision) return `Короткий код «${shortcut}» уже занят: «${collision.title || collision.shortcut}».`;
  return null;
}

/**
 * Human-readable message for a failed quick-reply request.
 *
 * The backend answers `409` for a duplicate shortcut, `404` for a vanished row
 * and `422` for a blank body (§5.2); each gets its own wording instead of a raw
 * `detail`, which for `422` is an array of validator objects.
 */
function quickReplyErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return "Такой короткий код уже используется — выберите другой.";
    if (error.status === 404) return "Быстрый ответ не найден — возможно, его уже удалили.";
    if (error.status === 422) {
      return error.message && error.message !== "Validation error"
        ? `Проверьте поля: ${error.message}`
        : "Проверьте поля: пустой текст или код недопустимого формата.";
    }
    return error.message;
  }
  return fallback;
}

/* ----------------------------------------------- CONTRACTS_V5 §1 — helpers */

/**
 * Human-readable message for a failed `DELETE /api/messages/{id}` (§1.3).
 *
 * `403` / `404` / `409` are the three documented domain answers; anything else
 * falls back to the transport message so a `500` is never swallowed.
 */
function deleteMessageErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Недостаточно прав: удаление сообщений запрещено для вашей учётной записи.";
    if (error.status === 404) return "Сообщение не найдено — возможно, история была перезагружена.";
    if (error.status === 409) return "Сообщение уже удалено — обновите чат, чтобы увидеть актуальное состояние.";
    if (error.status === 0) return "Нет связи с сервером — сообщение не удалено.";
    return error.message;
  }
  return "Не удалось удалить сообщение.";
}

/**
 * Two reactions in flight for one message would race (a `finally` could clear the
 * newer request's lock), so the store keeps one pending promise per message id.
 */
const reactionInFlight = new Map<number, Promise<void>>();

/**
 * Last confirmed server state per message, i.e. the aggregate that a rollback must
 * restore.
 *
 * The optimistic slice captured *before* a click is not a valid rollback target:
 * two clicks in a row (👍 then 🔥) each snapshot the already-painted optimistic
 * value, so a failing second `PUT` would "roll back" to the first optimistic guess
 * and leave the UI showing a vote the server never accepted. Remembering the last
 * confirmed aggregate keeps *other operators'* counters — and our own real vote —
 * exactly as the server reported them.
 */
const confirmedReactions = new Map<number, ReactionSummary[]>();

/** Cap the confirmation cache so a long session cannot grow it without bound. */
const CONFIRMED_REACTIONS_LIMIT = 500;

function rememberConfirmed(messageId: number, items: readonly ReactionSummary[]): void {
  if (confirmedReactions.size >= CONFIRMED_REACTIONS_LIMIT) {
    const oldest = confirmedReactions.keys().next();
    if (!oldest.done) confirmedReactions.delete(oldest.value);
  }
  confirmedReactions.set(
    messageId,
    items.map((item) => ({ ...item })),
  );
}

/** Baseline for an optimistic vote: the confirmed state, else what is on screen. */
function reactionBaseline(messageId: number, current: readonly ReactionSummary[]): ReactionSummary[] {
  const confirmed = confirmedReactions.get(messageId);
  return confirmed ? confirmed.map((item) => ({ ...item })) : [...current];
}

export type ToastKind = "info" | "success" | "warning" | "error";

export interface Toast {
  id: string;
  kind: ToastKind;
  message: string;
  at: number;
}

export interface TypingLockView {
  chat_id: number;
  operator_id: number;
  operator_name: string;
  until: string;
  /** Remaining ms, refreshed by the local ticker. */
  remaining_ms: number;
}

export interface TxLookupState {
  tx_id: string | null;
  loading: boolean;
  error: string | null;
  result: TxLookupResponse | null;
  source: "manual" | "click" | "auto";
  looked_up_at: number | null;
}

export type TxPanelKey = "one" | "two";

/**
 * CONTRACTS_V5 §6.3 — `selectChat` must not always wipe the timeline.
 *
 * `preserveContext` is used when the operator arrives from a global-search hit:
 * the target chat already has a window of messages loaded around the found one,
 * so the unconditional `loadMessages(chatId)` would replace it with the newest
 * page and the highlighted message would simply not be there.
 */
export interface SelectChatOptions {
  /** Skip the "load the latest page" step and keep `messagesByChat[chatId]` as-is. */
  preserveContext?: boolean;
}

/**
 * Full store shape (state + actions).
 *
 * Exported so components can read a dynamic key of it in a typed way
 * (the search overlay reads the flat hit list inside its key handler).
 */
export interface CRMState {
  /* session */
  mode: "live" | "mock";
  connection: ConnectionState;
  wsLatencyMs: number | null;
  authLoading: boolean;
  authError: string | null;
  operator: Operator | null;
  operators: Operator[];
  slaStats: PersonalSlaStats | null;

  /* chats */
  chats: UiChatItem[];
  filter: ChatFilter;
  selectedChatId: number | null;
  loadingChats: boolean;

  /* messages */
  messagesByChat: Record<number, Message[]>;
  loadingMessages: boolean;
  /** Message id the user is replying to (native reply). */
  replyTargetId: number | null;
  /** Composer text (lifted so the sidebar can inject tx summaries). */
  draft: string;
  /** Message id to flash/scroll to. */
  highlightMessageId: number | null;
  sending: boolean;

  /* typing collision lock */
  typingLocks: Record<number, TypingLockView>;
  myLockChatId: number | null;

  /* ------------------------------------- CONTRACTS_V3 §2/§3.2 — edits */
  /** Message id currently being edited in the composer, `null` = normal mode. */
  editingMessageId: number | null;
  /** Text shown in the textarea while editing (draft is untouched, see §3.2). */
  editingDraft: string;
  /** Per-message Telegram edit history, keyed by message id. */
  editHistory: Record<number, MessageEdit[]>;
  /** In-flight flag of the `GET /edits` request per message id. */
  editHistoryLoading: Record<number, boolean>;
  /** Last load error per message id (`null` = fine). */
  editHistoryError: Record<number, string | null>;
  /** Whether the `EditHistory` block is expanded per message id. */
  editHistoryOpen: Record<number, boolean>;

  /* --------------------------------- CONTRACTS_V3 §1 — forward dialog */
  /** Message being forwarded, `null` closes the dialog. */
  forwardSource: Message | null;
  forwardSubmitting: boolean;
  forwardError: string | null;

  /* ------------------------------- CONTRACTS_V5 §5 — editable quick replies */
  /** Replies sorted by `sort_order` then `id`, mirroring the server order (§5.2). */
  quickReplies: QuickReply[];
  /** `true` while the first `GET /api/quick-replies` is in flight. */
  quickRepliesLoading: boolean;
  /** Last load/save error, rendered by the editor instead of a silent empty list. */
  quickRepliesError: string | null;
  /** Id of the reply whose request is in flight (`null` = idle). */
  quickReplyPendingId: number | null;
  /** `true` while a create/update/delete request is on the wire. */
  quickReplyMutating: boolean;

  /* --------------------------------------- CONTRACTS_V5 §6 — global search */
  /** `true` while the Telegram-style search overlay is mounted. */
  searchOpen: boolean;
  /** Raw input value (debounced 250 ms before it reaches `runSearch`). */
  searchQuery: string;
  searchResults: SearchHit[];
  searchLoading: boolean;
  searchError: string | null;
  /** Sender filter forwarded to `GET /api/search` (§6.2). */
  searchSender: SearchSenderFilter;

  /* tx lookups */
  txOne: TxLookupState;
  txTwo: TxLookupState;

  /* ui */
  toasts: Toast[];

  /* actions */
  bootstrap: () => Promise<void>;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => void;
  setShift: (status: ShiftStatus) => Promise<void>;
  setFilter: (filter: ChatFilter) => void;
  /** CONTRACTS_V5 §6.3 — `preserveContext` keeps the pre-loaded timeline. */
  selectChat: (chatId: number | null, options?: SelectChatOptions) => Promise<void>;
  loadMessages: (chatId: number, before?: number) => Promise<void>;
  sendMessage: () => Promise<void>;
  /** CONTRACTS_V2 §3.1 — send files (optionally captioned) as one CRM message. */
  uploadFile: (chatId: number, files: File[], text?: string, replyToId?: number | null) => Promise<boolean>;
  setDraft: (text: string) => void;
  appendToDraft: (text: string) => void;
  setReplyTarget: (messageId: number | null) => void;
  jumpToMessage: (messageId: number) => void;
  clearHighlight: () => void;
  /* --------------------------------------- CONTRACTS_V5 §6 — global search */
  /** Open the overlay (Ctrl/⌘+K, or the chat-list search button). */
  openSearch: () => void;
  /** Close the overlay and cancel any debounced/in-flight request. */
  closeSearch: () => void;
  /** Debounced (250 ms) query update; a superseded request is aborted. */
  setSearchQuery: (query: string) => void;
  /** Execute the pending query immediately (Enter inside the overlay). */
  runSearch: () => Promise<void>;
  /** Reset results/error/query without closing the overlay. */
  clearSearch: () => void;
  /** `sender` filter of §6.2; re-runs the search when one is active. */
  setSearchSender: (sender: SearchSenderFilter) => void;
  /**
   * Jump from a search hit into its chat *without* losing the surrounding
   * history: the timeline is loaded around the message, merged, and flashed.
   */
  openMessageInChat: (chatId: number, messageId: number) => Promise<void>;
  beginTyping: (chatId: number) => void;
  endTyping: (chatId: number) => void;
  /** CONTRACTS_V2 §3.2 — optimistic reaction toggle (place / replace / remove). */
  putReaction: (messageId: number, emoji: string) => Promise<void>;
  removeReaction: (messageId: number) => Promise<void>;
  /* ------------------------------------- CONTRACTS_V3 §2/§3.2 — edits */
  /** Enter edit mode: seeds `editingDraft` from the message text. */
  startEditing: (message: Message) => void;
  /** Leave edit mode and drop the draft (chat switch, Esc, X). */
  cancelEditing: () => void;
  /** `PATCH /api/messages/{id}`; keeps edit mode on failure. */
  saveEdit: (text: string) => Promise<void>;
  /** Expand/collapse the history block, loading it on first open. */
  toggleEditHistory: (messageId: number) => void;
  /** `GET /api/messages/{id}/edits` — explicit loader (used by the toggle). */
  loadEditHistory: (messageId: number) => Promise<void>;
  /* --------------------------------- CONTRACTS_V3 §1 — forward dialog */
  openForwardDialog: (message: Message) => void;
  closeForwardDialog: () => void;
  /** `POST /api/messages/{id}/forward`; keeps the dialog open on failure. */
  forwardMessage: (targetChatId: number, comment?: string) => Promise<void>;
  /* ------------------------------------------ CONTRACTS_V5 §1 — deletion */
  /**
   * `DELETE /api/messages/{id}` — soft-delete and repaint the bubble in place.
   *
   * Resolves `true` only when the CRM row really flipped to
   * `is_deleted_in_tg = true`. Every documented failure (`403` / `404` / `409`)
   * is explained by a toast and resolves to `false` — it never throws, so the
   * caller can use it directly as an `onClick` handler.
   */
  deleteMessage: (messageId: number) => Promise<boolean>;
  /* -------------------------------- CONTRACTS_V5 §5 — editable quick replies */
  /** `GET /api/quick-replies`; idempotent, the editor and composer both call it. */
  loadQuickReplies: (options?: { force?: boolean }) => Promise<void>;
  /** `POST /api/quick-replies`; optimistic insert, rolled back on failure. */
  createQuickReply: (payload: QuickReplyCreatePayload) => Promise<QuickReply | null>;
  /** `PATCH /api/quick-replies/{id}`; optimistic patch, rolled back on failure. */
  updateQuickReply: (id: number, payload: QuickReplyUpdatePayload) => Promise<QuickReply | null>;
  /** `DELETE /api/quick-replies/{id}`; optimistic removal, restored on failure. */
  deleteQuickReply: (id: number) => Promise<boolean>;
  handleWsEnvelope: (envelope: WsEnvelope<unknown>) => void;
  lookupTx: (txId: string, opts?: { panel?: TxPanelKey; source?: TxLookupState["source"] }) => Promise<void>;
  insertTxIntoReply: (panel: TxPanelKey) => void;
  clearTx: (panel?: TxPanelKey) => void;
  pushToast: (kind: ToastKind, message: string) => void;
  dismissToast: (id: string) => void;
  tick: () => void;
}

/* ------------------------------------------------------------------ utils */

function computeUiChat(chat: ChatListItem, now: number): UiChatItem {
  const remaining = chat.sla ? secondsUntil(chat.sla.deadline_at, now) : null;
  return {
    ...chat,
    sla_remaining_seconds: remaining,
    sla_local: remaining !== null,
  } as UiChatItem;
}

function emptyTxState(): TxLookupState {
  return { tx_id: null, loading: false, error: null, result: null, source: "manual", looked_up_at: null };
}

function matchFilter(chat: UiChatItem, filter: ChatFilter, operatorId: number | null): boolean {
  if (filter === "mine") return operatorId !== null && chat.assigned_operator_id === operatorId;
  if (filter === "burning") {
    const remaining = chat.sla_remaining_seconds;
    return chat.sla !== null && existingSlaPending(chat) && (remaining === null || remaining <= 60);
  }
  return true;
}

function existingSlaPending(chat: UiChatItem): boolean {
  return chat.sla !== null && chat.sla.status === "PENDING";
}

function sortChats(chats: UiChatItem[]): UiChatItem[] {
  return [...chats].sort((left, right) => {
    const leftTime = left.last_message?.created_at ?? "";
    const rightTime = right.last_message?.created_at ?? "";
    return rightTime.localeCompare(leftTime);
  });
}

function mergeMessage(list: Message[], message: Message): Message[] {
  const index = list.findIndex((item) => item.id === message.id);
  if (index === -1) {
    return [...list, message].sort((left, right) => left.id - right.id);
  }
  const next = [...list];
  next[index] = { ...next[index], ...message };
  return next;
}

/**
 * Merge a server-sourced message list and seed the reaction baseline.
 *
 * Every message that arrives from REST/WS carries the authoritative aggregate, so
 * this is the moment to remember it as the rollback target for later optimistic
 * votes. Optimistic ids are skipped (they have no server state to confirm).
 */
function mergeServerMessages(list: Message[], incoming: readonly Message[]): Message[] {
  let merged = list;
  for (const message of incoming) {
    merged = mergeMessage(merged, message);
    if (!isOptimisticId(message.id) && Array.isArray(message.reactions)) {
      rememberConfirmed(message.id, message.reactions);
    }
  }
  return merged;
}

/* -------------------------------------------------------- media/reactions */

/** An optimistic message id is negative (`-Date.now()`), the server never sends those. */
function isOptimisticId(messageId: number): boolean {
  return !Number.isFinite(messageId) || messageId < 0;
}

interface MessageLocation {
  chat_id: number;
  message: Message;
}

/**
 * Find a message by id across *all* loaded chats.
 *
 * The reaction frames (§4) carry `chat_id` as a hint, but a chat may have been
 * reloaded or never opened locally, so the lookup never trusts that hint alone.
 * Complexity is O(messages) with two early exits (hinted chat first, then the
 * remaining chats), and `null` is returned for unknown/optimistic ids.
 */
function findMessageById(messagesByChat: Record<number, Message[]>, messageId: number, chatHint?: number): MessageLocation | null {
  if (isOptimisticId(messageId)) return null;

  const hinted = chatHint === undefined ? undefined : messagesByChat[chatHint];
  if (hinted?.length) {
    const found = hinted.find((item) => item.id === messageId);
    if (found) return { chat_id: chatHint as number, message: found };
  }

  for (const chatId of Object.keys(messagesByChat)) {
    const numericChatId = Number(chatId);
    if (numericChatId === chatHint) continue;
    const list = messagesByChat[numericChatId];
    if (!list?.length) continue;
    const found = list.find((item) => item.id === messageId);
    if (found) return { chat_id: numericChatId, message: found };
  }
  return null;
}

/** Emoji ordering used by the reaction bar; `null` means "keep the server order". */
function reactionOrder(emoji: string, position: number): [number, number, string] {
  const index = REACTION_ORDER.indexOf(emoji);
  return [index === -1 ? REACTION_ORDER.length : index, position, emoji];
}

function sortReactions(reactions: ReactionSummary[]): ReactionSummary[] {
  return reactions
    .map((item, position) => ({ item, key: reactionOrder(item.emoji, position) }))
    .sort((left, right) => {
      if (left.key[0] !== right.key[0]) return left.key[0] - right.key[0];
      if (left.key[1] !== right.key[1]) return left.key[1] - right.key[1];
      return left.key[2].localeCompare(right.key[2]);
    })
    .map((entry) => entry.item);
}

function summaryOf(emoji: string, count: number, me: boolean, previous?: ReactionSummary): ReactionSummary {
  return {
    emoji,
    count,
    me,
    operators: previous?.operators ?? [],
    operator_ids: previous?.operator_ids ?? [],
  };
}

/**
 * Pure `PUT /reaction` simulation: drop *every* bucket the caller owns (one
 * operator — one reaction, §3.2) and bump `emoji`.
 *
 * The caller's own vote is always *moved* to `emoji`, so a bucket they held
 * before loses exactly one vote and keeps the other operators' counters.
 */
function reactionAfterPut(reactions: readonly ReactionSummary[], emoji: string): ReactionSummary[] {
  const next: ReactionSummary[] = [];
  const previous = reactions.find((item) => item.emoji === emoji);

  for (const item of reactions) {
    if (!item.me) {
      next.push(item);
      continue;
    }
    const remaining = Math.max(0, item.count - 1);
    if (remaining === 0) continue;
    next.push(summaryOf(item.emoji, remaining, false, item));
  }

  /* `next` now holds only other people's votes; add mine back on `emoji`. */
  const index = next.findIndex((item) => item.emoji === emoji);
  const base = index === -1 ? 0 : next[index].count;
  const bumped = summaryOf(emoji, base + 1, true, previous);
  if (index === -1) next.push(bumped);
  else next[index] = bumped;

  return sortReactions(next.filter((item) => item.count > 0));
}

/** Pure `DELETE /reaction` simulation: remove the caller from every bucket. */
function reactionAfterRemove(reactions: readonly ReactionSummary[]): ReactionSummary[] {
  const next: ReactionSummary[] = [];
  for (const item of reactions) {
    if (!item.me) {
      next.push(item);
      continue;
    }
    const remaining = Math.max(0, item.count - 1);
    if (remaining > 0) next.push(summaryOf(item.emoji, remaining, false, item));
  }
  return sortReactions(next);
}

/** Mirrors the six `ReactionEmoji` values of CONTRACTS_V2 §1. */
const REACTION_ORDER: readonly string[] = ["👍", "❤️", "🔥", "👀", "👏", "🙏"];

/**
 * Stable empty reaction array shared by every "no reactions yet" message.
 * Exported so components can use it as a default value without allocating.
 */
export const EMPTY_REACTIONS: ReactionSummary[] = [];

/** Stable empty attachment array (same reasoning as `EMPTY_REACTIONS`). */
export const EMPTY_ATTACHMENTS: Attachment[] = [];

/**
 * Stable empty edit history (CONTRACTS_V3 §2). Components default to this in
 * `useCRMStore(s => s.editHistory[id] ?? EMPTY_EDITS)`: a fresh `[]` would
 * change identity on every read and make `useSyncExternalStore` loop forever.
 */
export const EMPTY_EDITS: MessageEdit[] = [];

/**
 * Stable empty quick-reply list (CONTRACTS_V5 §5).
 *
 * `selectActiveQuickReplies` returns this before touching the store array so a
 * not-yet-loaded list keeps a single identity across renders.
 */
export const EMPTY_QUICK_REPLIES: QuickReply[] = [];

/**
 * Replace `reactions` of one message, looking it up by id across every chat.
 *
 * Used both by the REST confirmation path (authoritative aggregate) and by a
 * rollback (the exact slice captured before the optimistic paint).
 */
function replaceMessageReactions(
  set: SetFn,
  chatHint: number,
  messageId: number,
  items: readonly ReactionSummary[],
): void {
  set((state) => {
    const location = findMessageById(state.messagesByChat, messageId, chatHint);
    if (!location) return {};
    const list = state.messagesByChat[location.chat_id];
    if (!list) return {};
    const next = sortReactions(items.map((item) => ({ ...item })));
    return {
      messagesByChat: {
        ...state.messagesByChat,
        [location.chat_id]: list.map((message) =>
          message.id === messageId ? { ...message, reactions: next.length > 0 ? next : EMPTY_REACTIONS } : message,
        ),
      },
    };
  });
}

/**
 * Apply one `MESSAGE_REACTION` / `MESSAGE_REACTION_REMOVED` frame.
 *
 * - `upsert` uses the frame's `count` as the absolute truth for that emoji
 *   (missing `count` falls back to "keep the current value, at most +1");
 * - `remove` decrements by one, dropping the bucket at zero;
 * - `me` is recomputed from `operator_id`: our own frames (echoed by the server
 *   to every socket) keep the flag, foreign frames leave it untouched — the
 *   next `GET /reactions` or REST response reconciles it exactly.
 */
function applyReactionFrame(
  set: SetFn,
  payload: WsMessageReactionData | null | undefined,
  mode: "upsert" | "remove",
): void {
  if (!payload) return;
  const messageId = Number(payload.message_id);
  const emoji = typeof payload.emoji === "string" ? payload.emoji : "";
  if (!Number.isFinite(messageId) || !emoji) return;

  set((state) => {
    const chatHint = Number(payload.chat_id);
    const location = findMessageById(
      state.messagesByChat,
      messageId,
      Number.isFinite(chatHint) ? chatHint : undefined,
    );
    if (!location) return {};
    const list = state.messagesByChat[location.chat_id];
    if (!list) return {};

    const mine = payload.operator_id !== null && payload.operator_id !== undefined && payload.operator_id === state.operator?.id;
    const current = location.message.reactions ?? EMPTY_REACTIONS;
    const existing = current.find((item) => item.emoji === emoji);
    const next: ReactionSummary[] = [];

    for (const item of current) {
      if (item.emoji !== emoji) {
        next.push(item);
        continue;
      }
      if (mode === "remove") {
        const count = Math.max(0, item.count - 1);
        if (count === 0) continue;
        next.push(summaryOf(item.emoji, count, mine ? false : item.me, item));
        continue;
      }
      const frameCount = typeof payload.count === "number" && Number.isFinite(payload.count) ? payload.count : null;
      const count = Math.max(0, frameCount ?? item.count + 1);
      if (count === 0) continue;
      next.push(summaryOf(item.emoji, count, mine ? true : item.me, item));
    }

    if (mode === "upsert" && !existing) {
      const frameCount = typeof payload.count === "number" && Number.isFinite(payload.count) ? payload.count : null;
      const count = Math.max(1, frameCount ?? 1);
      next.push(summaryOf(emoji, count, mine, undefined));
    }

    const sorted = sortReactions(next);
    /* Skip the store write entirely when nothing observable changed: `set` would
       re-render every subscriber of the timeline for a repeated frame. */
    const signature = (list: readonly ReactionSummary[]): string =>
      [...list]
        .map((item) => `${item.emoji}:${item.count}:${item.me ? 1 : 0}`)
        .sort()
        .join("|");
    if (signature(sorted) === signature(current)) return {};

    return {
      messagesByChat: {
        ...state.messagesByChat,
        [location.chat_id]: list.map((message) =>
          message.id === messageId ? { ...message, reactions: sorted.length > 0 ? sorted : EMPTY_REACTIONS } : message,
        ),
      },
    };
  });
}

/** True when the payload carries a usable attachment array (`null` when absent/invalid). */
function asAttachments(value: unknown): Attachment[] | null {
  if (!Array.isArray(value)) return null;
  const items = value.filter(
    (item): item is Attachment =>
      typeof item === "object" && item !== null && typeof (item as { id?: unknown }).id === "number",
  );
  return items.map((item) => ({ ...item, url: item.url ?? "" }));
}

/** Derive the `kind` of a freshly uploaded message from its attachments. */
function kindOfAttachments(attachments: readonly Attachment[]): MessageKind {
  const first = attachments[0];
  if (!first) return "TEXT";
  const candidate = first.kind as MessageKind;
  return MESSAGE_KINDS.includes(candidate) ? candidate : "OTHER";
}

/** Guess the attachment kind from the browser's `File` (no backend round trip). */
function guessKind(file: File): MessageKind {
  const mime = file.type.toLowerCase();
  const name = file.name.toLowerCase();
  if (mime.startsWith("image/")) {
    if (name.endsWith(".webp") && mime === "image/webp" && /sticker|аниме/.test(name)) return "STICKER";
    return "PHOTO";
  }
  if (mime.startsWith("video/")) return "VIDEO";
  if (mime.startsWith("audio/")) return mime.includes("ogg") || mime.includes("opus") ? "VOICE" : "AUDIO";
  return "DOCUMENT";
}

/**
 * Locally fabricated media message for the mock/demo mode.
 *
 * `URL.createObjectURL` is used so the picked file is actually previewable. Every
 * blob URL is tracked so it can be revoked in *three* situations:
 *
 * 1. after `MOCK_BLOB_TTL_MS` (the preview had its minute even if the message is
 *    still on screen),
 * 2. when the page unloads (`pagehide`/`beforeunload`), so a closed tab does not
 *    pin the `File` handle forever,
 * 3. eagerly, when the demo dataset is reset (`revokeAllMockBlobs`).
 *
 * The previous implementation only covered (1) and — worse — scheduled one timer
 * per picked file while never cancelling it, so repeated picks stacked timers that
 * outlived the component tree.
 */
const MOCK_BLOB_TTL_MS = 60_000;
const mockBlobTimers = new Map<string, number>();
const mockBlobUrls = new Set<string>();

function revokeMockBlob(url: string): void {
  const timer = mockBlobTimers.get(url);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    mockBlobTimers.delete(url);
  }
  if (mockBlobUrls.delete(url)) {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* some browsers throw on an already-revoked URL */
    }
  }
}

/** Revoke every live mock blob URL (called on logout and on page unload). */
export function revokeAllMockBlobs(): void {
  for (const url of [...mockBlobUrls]) revokeMockBlob(url);
}

let unloadGuardInstalled = false;

/** Install the page-unload guard exactly once, lazily, on the first mock upload. */
function installMockBlobUnloadGuard(): void {
  if (unloadGuardInstalled || typeof window === "undefined") return;
  unloadGuardInstalled = true;
  const release = (): void => revokeAllMockBlobs();
  window.addEventListener("pagehide", release);
  window.addEventListener("beforeunload", release);
}

function trackMockBlob(url: string): void {
  if (!url.startsWith("blob:")) return;
  mockBlobUrls.add(url);
  const timer = window.setTimeout(() => revokeMockBlob(url), MOCK_BLOB_TTL_MS);
  mockBlobTimers.set(url, timer);
  installMockBlobUnloadGuard();
}

function buildLocalMediaMessage(args: {
  chatId: number;
  operator: Operator;
  caption: string;
  replyToId: number | null;
  replyToTgMsgId: number | null;
  files: readonly File[];
}): Message {
  const { chatId, operator, caption, replyToId, replyToTgMsgId, files } = args;
  /* One id for the message *and* its attachments: two independent
     `-Math.abs(Date.now())` calls in the same millisecond used to disagree, so the
     attachment rows pointed at a message id that did not exist. */
  const messageId = -Math.abs(Date.now());
  const token = uuid();
  const attachments: Attachment[] = files.map((file, index) => {
    const url = typeof URL.createObjectURL === "function" ? URL.createObjectURL(file) : "";
    trackMockBlob(url);
    return {
      id: index + 1,
      message_id: messageId,
      kind: guessKind(file),
      file_name: file.name,
      mime_type: file.type || null,
      size_bytes: file.size,
      width: null,
      height: null,
      duration_seconds: null,
      url,
      thumbnail_url: null,
    };
  });

  return {
    id: messageId,
    chat_id: chatId,
    tg_msg_id: null,
    sender_type: "OPERATOR",
    sender_operator_id: operator.id,
    sender_name: operator.display_name,
    sender_tg_id: null,
    text: caption,
    reply_to_id: replyToId,
    reply_to_tg_msg_id: replyToTgMsgId,
    phone: null,
    is_deleted_in_tg: false,
    deleted_at: null,
    sla_incident_id: null,
    delivery_status: "sent",
    is_outgoing: true,
    client_nonce: token,
    created_at: new Date().toISOString(),
    kind: kindOfAttachments(attachments),
    caption: caption || null,
    attachments,
    reactions: [],
  };
}

/**
 * Serialize reaction writes per message.
 *
 * `next()` runs only after the previous request for the same message settled, so
 * two rapid clicks cannot interleave (last-write-wins on the server) and the
 * helper returns the *final* settled state to the caller.
 */
function enqueueReaction(messageId: number, task: () => Promise<void>): Promise<void> {
  const previous = reactionInFlight.get(messageId) ?? Promise.resolve();
  const chained = previous.then(task, task).finally(() => {
    if (reactionInFlight.get(messageId) === chained) reactionInFlight.delete(messageId);
  });
  reactionInFlight.set(messageId, chained);
  return chained;
}

/* -------------------------------------------------- CONTRACTS_V3 §2 — edits */

/**
 * Coerce an arbitrary WS/`GET` payload into a `MessageEdit`, or `null`.
 *
 * The backend is the single source of truth, but a socket frame is untrusted
 * input: a malformed edit must never reach `editHistory` and break the list.
 */
function asMessageEdit(value: unknown): MessageEdit | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Partial<MessageEdit>;
  if (typeof candidate.id !== "number" || !Number.isFinite(candidate.id)) return null;
  if (typeof candidate.message_id !== "number" || !Number.isFinite(candidate.message_id)) return null;
  if (typeof candidate.created_at !== "string") return null;
  const source = candidate.source === "CRM" ? "CRM" : "TELEGRAM";
  return {
    id: candidate.id,
    message_id: candidate.message_id,
    source,
    old_text: typeof candidate.old_text === "string" ? candidate.old_text : "",
    new_text: typeof candidate.new_text === "string" ? candidate.new_text : "",
    operator_id: typeof candidate.operator_id === "number" ? candidate.operator_id : null,
    actor_name: typeof candidate.actor_name === "string" ? candidate.actor_name : null,
    created_at: candidate.created_at,
  };
}

/** Sanitize a `GET /edits` response; `null` when the shape is not an array. */
function asMessageEdits(value: unknown): MessageEdit[] | null {
  if (!Array.isArray(value)) return null;
  const items: MessageEdit[] = [];
  for (const raw of value) {
    const edit = asMessageEdit(raw);
    if (edit) items.push(edit);
  }
  return items;
}

/**
 * Append `edit` unless an entry with the same `id` is already known.
 *
 * `MESSAGE_EDITED` is broadcast to *every* socket and may be re-delivered after
 * a reconnect, so the history must be idempotent by `id` (§2.6).
 */
function withEdit(history: Record<number, MessageEdit[]>, edit: MessageEdit): Record<number, MessageEdit[]> {
  const current = history[edit.message_id] ?? EMPTY_EDITS;
  if (current.some((item) => item.id === edit.id)) return history;
  const next = [...current, edit].sort((left, right) => left.id - right.id);
  return { ...history, [edit.message_id]: next };
}

/* ------------------------------------------- CONTRACTS_V3 §1 — forwarding */

/**
 * Merge a forwarded copy into the target timeline — only when that chat is
 * already loaded. Loading it on demand would fabricate a busy state for a chat
 * the operator is not looking at; the next `loadMessages` picks it up anyway.
 */
function mergeIntoLoadedChat(
  messagesByChat: Record<number, Message[]>,
  chatId: number,
  message: Message,
): Record<number, Message[]> {
  if (!Array.isArray(messagesByChat[chatId])) return messagesByChat;
  return { ...messagesByChat, [chatId]: mergeMessage(messagesByChat[chatId], message) };
}

let wsClient: WsClient | null = null;
let heartbeatTimer: number | null = null;

/* --------------------------------------------- CONTRACTS_V5 §6 — search */

/** Debounce window before a keystroke turns into a request (§6.3). */
const SEARCH_DEBOUNCE_MS = 250;
/** `limit=20` — exactly what the backend contract documents (§6.2). */
const SEARCH_PAGE_LIMIT = 20;
/** Server-side cap of `q` (§6.2: 1..200). */
const MAX_SEARCH_QUERY_LENGTH = 200;
/** Messages fetched on each side of the hit when the chat is cold. */
const SEARCH_CONTEXT_LIMIT = 50;

let searchTimer: number | null = null;
let searchAbort: AbortController | null = null;
/** Monotonic stamp: only the newest request is allowed to write its results. */
let searchRequestSeq = 0;

function cancelPendingSearch(): void {
  if (searchTimer !== null) {
    window.clearTimeout(searchTimer);
    searchTimer = null;
  }
  if (searchAbort !== null) {
    searchAbort.abort();
    searchAbort = null;
  }
  searchRequestSeq += 1;
}

/** Coerce an untrusted `match_spans` payload into sorted, in-range pairs. */
function asMatchSpans(value: unknown, length: number): [number, number][] {
  if (!Array.isArray(value)) return [];
  const spans: [number, number][] = [];
  for (const raw of value) {
    if (!Array.isArray(raw) || raw.length < 2) continue;
    const start = Number(raw[0]);
    const end = Number(raw[1]);
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    const from = Math.max(0, Math.min(length, Math.trunc(start)));
    const to = Math.max(from, Math.min(length, Math.trunc(end)));
    if (to > from) spans.push([from, to]);
  }
  return spans.sort((left, right) => left[0] - right[0]);
}

/** Sanitize one `SearchHitOut`; `null` when it cannot be rendered. */
function asSearchHit(value: unknown): SearchHit | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Partial<SearchHit> & { message?: unknown };
  const chatId = Number(candidate.chat_id);
  const message = candidate.message as Message | undefined;
  if (!Number.isFinite(chatId)) return null;
  if (!message || typeof message !== "object") return null;
  const messageId = Number(message.id);
  if (!Number.isFinite(messageId)) return null;

  const text = typeof message.text === "string" ? message.text : "";
  const caption = typeof message.caption === "string" ? message.caption : "";
  const snippet =
    typeof candidate.snippet === "string" && candidate.snippet.length > 0
      ? candidate.snippet
      : (caption || text || "(без текста)").slice(0, 160);

  return {
    chat_id: chatId,
    chat_title: typeof candidate.chat_title === "string" ? candidate.chat_title : "",
    chat_merchant_code: typeof candidate.chat_merchant_code === "string" ? candidate.chat_merchant_code : null,
    message: { ...message, chat_id: chatId },
    snippet,
    match_spans: asMatchSpans(candidate.match_spans, snippet.length),
    score: Number.isFinite(candidate.score) ? Number(candidate.score) : 0,
  };
}

/**
 * Local substring scan used in demo mode, where there is no `/api/search`.
 *
 * `toLowerCase` on both sides gives the same case-insensitive behaviour the
 * backend gets from `pg_trgm` (invariant §8.8: substring, not word matching).
 */
function searchMockMessages(
  messagesByChat: Record<number, Message[]>,
  chats: readonly UiChatItem[],
  query: string,
): SearchHit[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const titles = new Map(chats.map((chat) => [chat.id, chat]));
  const hits: SearchHit[] = [];

  for (const [key, list] of Object.entries(messagesByChat)) {
    const chatId = Number(key);
    const chat = titles.get(chatId);
    for (const message of list) {
      const body = message.caption?.trim() || message.text || "";
      const index = body.toLowerCase().indexOf(needle);
      if (index === -1) continue;
      const from = Math.max(0, index - 40);
      const to = Math.min(body.length, index + needle.length + 60);
      const prefix = from > 0 ? "…" : "";
      const snippet = `${prefix}${body.slice(from, to)}${to < body.length ? "…" : ""}`;
      const start = prefix.length + (index - from);
      hits.push({
        chat_id: chatId,
        chat_title: chat?.title ?? `Chat ${chatId}`,
        chat_merchant_code: chat?.merchant_code ?? null,
        message,
        snippet,
        match_spans: [[start, start + needle.length]],
        score: 1,
      });
    }
  }

  return hits.sort((left, right) => right.message.created_at.localeCompare(left.message.created_at));
}

/**
 * Load a window of history around `messageId` and stitch it into the timeline.
 *
 * `GET /messages?before_id=N` answers "the newest `limit` messages older than
 * N", so one call *below* the id and one from the newest page cover the window
 * from both sides. Everything is merged through `mergeServerMessages`, which is
 * idempotent by id — the operator's already-loaded messages (and optimistic
 * ones) are never lost, they are only enriched.
 */
async function loadContextAround(chatId: number, messageId: number): Promise<void> {
  const [below, above] = await Promise.allSettled([
    api.listMessages(chatId, SEARCH_CONTEXT_LIMIT, messageId + 1),
    api.listMessages(chatId, SEARCH_CONTEXT_LIMIT),
  ]);

  const collected: Message[] = [];
  if (below.status === "fulfilled" && Array.isArray(below.value.items)) collected.push(...below.value.items);
  if (above.status === "fulfilled" && Array.isArray(above.value.items)) collected.push(...above.value.items);

  const failure = below.status === "rejected" ? below.reason : above.status === "rejected" ? above.reason : null;
  if (collected.length === 0 && failure) throw failure;

  useCRMStore.setState((state) => ({
    messagesByChat: {
      ...state.messagesByChat,
      [chatId]: mergeServerMessages(state.messagesByChat[chatId] ?? [], collected),
    },
  }));
}

/** Wait (briefly) for `#msg-<id>` to exist so the flash lands on a real node. */
function waitForMessageNode(messageId: number, timeoutMs = 4000): Promise<boolean> {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const check = (): void => {
      if (document.getElementById(`msg-${messageId}`)) {
        resolve(true);
        return;
      }
      if (Date.now() >= deadline) {
        resolve(false);
        return;
      }
      window.setTimeout(check, 60);
    };
    check();
  });
}


/* ------------------------------------------------------------------ store */

export const useCRMStore = create<CRMState>((set, get) => ({
  mode: FORCED_MOCK ? "mock" : "live",
  connection: "connecting",
  wsLatencyMs: null,
  authLoading: false,
  authError: null,
  operator: null,
  operators: [],
  slaStats: null,

  chats: [],
  filter: "all",
  selectedChatId: null,
  loadingChats: false,

  messagesByChat: {},
  loadingMessages: false,
  replyTargetId: null,
  draft: "",
  highlightMessageId: null,
  sending: false,

  typingLocks: {},
  myLockChatId: null,

  /* CONTRACTS_V3 §3.2 — edit mode (one message at a time across the whole app) */
  editingMessageId: null,
  editingDraft: "",
  editHistory: {},
  editHistoryLoading: {},
  editHistoryError: {},
  editHistoryOpen: {},

  /* CONTRACTS_V3 §1 — forward dialog */
  forwardSource: null,
  forwardSubmitting: false,
  forwardError: null,

  /* CONTRACTS_V5 §5 — editable quick replies */
  quickReplies: [],
  quickRepliesLoading: false,
  quickRepliesError: null,
  quickReplyPendingId: null,
  quickReplyMutating: false,

  txOne: emptyTxState(),
  txTwo: emptyTxState(),

  /* CONTRACTS_V5 §6 — global search */
  searchOpen: false,
  searchQuery: "",
  searchResults: [],
  searchLoading: false,
  searchError: null,
  searchSender: "all",

  toasts: [],

  /* ------------------------------------------------------------ bootstrap */

  bootstrap: async () => {
    const storedToken = window.localStorage.getItem(TOKEN_KEY);
    const storedOperator = window.localStorage.getItem(OPERATOR_KEY);

    if (FORCED_MOCK || !storedToken) {
      if (FORCED_MOCK) {
        activateMock(set, get);
        get().pushToast("info", "Mock mode — running on the bundled demo dataset.");
        return;
      }
      set({ connection: "idle" as unknown as ConnectionState });
      return;
    }

    api.setToken(storedToken);
    try {
      const me = await api.me();
      set({ operator: me, mode: "live" });
      await hydrate(set, get);
      connectWs(get, set);
    } catch {
      window.localStorage.removeItem(TOKEN_KEY);
      api.setToken(null);
      set({ operator: storedOperator ? null : null, connection: "closed" });
      if (!storedOperator) activateMock(set, get);
    }
  },

  /* ---------------------------------------------------------------- auth */

  login: async (username, password) => {
    set({ authLoading: true, authError: null });
    try {
      const response = await api.login(username, password);
      api.setToken(response.access_token);
      window.localStorage.setItem(TOKEN_KEY, response.access_token);
      window.localStorage.setItem(OPERATOR_KEY, JSON.stringify(response.operator));
      set({ operator: response.operator, mode: "live", authLoading: false, authError: null });
      await hydrate(set, get);
      connectWs(get, set);
      return true;
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "Login failed";
      set({ authLoading: false, authError: message });
      return false;
    }
  },

  logout: () => {
    const { mode, myLockChatId } = get();
    if (mode === "live") {
      if (myLockChatId !== null) wsClient?.unlockChat(myLockChatId);
      void api.logout().catch(() => undefined);
    }
    wsClient?.disconnect();
    wsClient = null;
    if (heartbeatTimer !== null) {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(OPERATOR_KEY);
    api.setToken(null);
    /* Demo blob URLs and cached reaction baselines belong to the session that is
       ending — releasing them here keeps a logout from leaking `File` handles. */
    revokeAllMockBlobs();
    confirmedReactions.clear();
    reactionInFlight.clear();
    cancelPendingSearch();
    set({
      operator: null,
      chats: [],
      operators: [],
      messagesByChat: {},
      selectedChatId: null,
      typingLocks: {},
      myLockChatId: null,
      draft: "",
      replyTargetId: null,
      slaStats: null,
      connection: "closed",
      /* CONTRACTS_V3 — no editor/dialog/history may survive the session. */
      editingMessageId: null,
      editingDraft: "",
      editHistory: {},
      editHistoryLoading: {},
      editHistoryError: {},
      editHistoryOpen: {},
      forwardSource: null,
      forwardSubmitting: false,
      forwardError: null,
      /* CONTRACTS_V5 §6 — no search session may outlive the operator. */
      searchOpen: false,
      searchQuery: "",
      searchResults: [],
      searchLoading: false,
      searchError: null,
    });
  },

  /* --------------------------------------------------------------- shift */

  setShift: async (status) => {
    const { operator, mode } = get();
    if (!operator) return;
    if (mode === "mock") {
      set({ operator: { ...operator, shift_status: status } });
      get().pushToast("warning", `Mock mode: shift set to ${status} (not persisted).`);
      return;
    }
    try {
      const updated = await api.setShiftStatus(status);
      set({ operator: updated });
      get().pushToast(
        status === "ONLINE" ? "success" : "warning",
        status === "ONLINE"
          ? "You are ONLINE — SLA incidents will be round-robin assigned to you."
          : `Shift set to ${status}. New incidents will skip you.`,
      );
      const stats = await api.slaStats().catch(() => null);
      if (stats) set({ slaStats: stats });
    } catch (error) {
      get().pushToast("error", error instanceof ApiError ? error.message : "Could not update shift status");
    }
  },

  /* --------------------------------------------------------------- chats */

  setFilter: (filter) => {
    set({ filter });
    wsClient?.setFilter(filter);
  },

  selectChat: async (chatId, options) => {
    /* CONTRACTS_V5 §6.3 — `preserveContext` is set when we arrive from a search
       hit whose window of history is already stitched into `messagesByChat`.
       Without it the unconditional `loadMessages` below replaces that window
       with the newest page, and the message we just jumped to disappears. */
    const preserveContext = options?.preserveContext === true;
    const previous = get().selectedChatId;
    if (previous !== null && previous !== chatId) {
      get().endTyping(previous);
    }
    /* Edit mode is scoped to the message's chat: leaving the chat must not leave
       the composer stuck in "editing" for a message that is no longer visible. */
    if (previous !== chatId && get().editingMessageId !== null) get().cancelEditing();
    set({ selectedChatId: chatId, replyTargetId: null, draft: "" });
    if (chatId === null) return;

    wsClient?.subscribe(chatId);
    set((state) => ({
      chats: state.chats.map((chat) => (chat.id === chatId ? { ...chat, unread_count: 0 } : chat)),
    }));

    /* A chat we are entering for the first time must still get a timeline; only
       a *cold* preserved chat is left alone (the caller is loading its own window). */
    const loaded = (get().messagesByChat[chatId] ?? []).length > 0;
    if (!preserveContext || !loaded) {
      await get().loadMessages(chatId);
    }

    if (get().mode === "live") {
      void api.markRead(chatId).catch(() => undefined);
    }
  },

  loadMessages: async (chatId, before) => {
    const { mode } = get();
    set({ loadingMessages: true });
    try {
      if (mode === "mock") {
        const dataset = createMockDataset();
        const existing = get().messagesByChat[chatId] ?? [];
        const incoming = dataset.messagesByChat[chatId] ?? [];
        const merged = existing.reduce<Message[]>((acc, message) => mergeMessage(acc, message), incoming);
        set((state) => ({ messagesByChat: { ...state.messagesByChat, [chatId]: merged } }));
        return;
      }
      const response = await api.listMessages(chatId, 100, before);
      set((state) => {
        const existing = before === undefined ? [] : (state.messagesByChat[chatId] ?? []);
        const merged = mergeServerMessages(existing, response.items);
        return { messagesByChat: { ...state.messagesByChat, [chatId]: merged } };
      });
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError) get().logout();
      get().pushToast("error", error instanceof ApiError ? error.message : "Could not load messages");
    } finally {
      set({ loadingMessages: false });
    }
  },

  /* ------------------------------------------------------------ messaging */

  setDraft: (text) => set({ draft: text }),

  appendToDraft: (text) =>
    set((state) => {
      const trimmed = state.draft.trimEnd();
      return { draft: trimmed ? `${trimmed}\n${text}` : text };
    }),

  setReplyTarget: (messageId) => set({ replyTargetId: messageId }),

  jumpToMessage: (messageId) => {
    set({ highlightMessageId: messageId });
    window.setTimeout(() => {
      if (get().highlightMessageId === messageId) set({ highlightMessageId: null });
    }, 2000);
  },

  clearHighlight: () => set({ highlightMessageId: null }),

  /* --------------------------------------- CONTRACTS_V5 §6 — global search */

  openSearch: () => set({ searchOpen: true }),

  closeSearch: () => {
    /* Dropping the timer *and* aborting the socket call: a request fired 200 ms
       ago would otherwise resolve after the overlay is gone and repopulate the
       store with results nobody can see. */
    cancelPendingSearch();
    set({ searchOpen: false, searchLoading: false });
  },

  setSearchQuery: (query) => {
    const next = query.slice(0, MAX_SEARCH_QUERY_LENGTH);
    set({ searchQuery: next });
    cancelPendingSearch();

    const trimmed = next.trim();
    if (!trimmed) {
      /* An emptied box clears the list immediately — no request, no spinner. */
      set({ searchResults: [], searchLoading: false, searchError: null });
      return;
    }

    /* Optimistic spinner: the box reacts before the debounce elapses instead of
       showing a 250 ms dead gap. */
    set({ searchLoading: true, searchError: null });
    searchTimer = window.setTimeout(() => {
      searchTimer = null;
      void useCRMStore.getState().runSearch();
    }, SEARCH_DEBOUNCE_MS);
  },

  runSearch: async () => {
    /* An explicit `runSearch` (Enter / sender toggle) supersedes a pending debounce. */
    if (searchTimer !== null) {
      window.clearTimeout(searchTimer);
      searchTimer = null;
    }
    if (searchAbort !== null) searchAbort.abort();

    const { searchQuery, searchSender, mode } = get();
    const query = searchQuery.trim();
    if (!query) {
      set({ searchResults: [], searchLoading: false, searchError: null });
      return;
    }

    const seq = (searchRequestSeq += 1);
    const controller = new AbortController();
    searchAbort = controller;
    set({ searchLoading: true, searchError: null });

    try {
      if (mode === "mock") {
        const { messagesByChat, chats } = get();
        const items = searchMockMessages(messagesByChat, chats, query);
        if (seq !== searchRequestSeq) return;
        set({ searchResults: items, searchLoading: false, searchError: null });
        return;
      }

      const response: SearchResponse = await api.searchMessages({
        q: query,
        limit: SEARCH_PAGE_LIMIT,
        sender: searchSender,
        signal: controller.signal,
      });

      /* Stale-request guard: a newer keystroke already owns the screen. */
      if (seq !== searchRequestSeq) return;

      const items = Array.isArray(response?.items)
        ? response.items.map(asSearchHit).filter((hit): hit is SearchHit => hit !== null)
        : [];
      set({ searchResults: items, searchLoading: false, searchError: null });
    } catch (error) {
      if (seq !== searchRequestSeq) return;
      if (error instanceof ApiError && error.status === 408) {
        /* Our own AbortController fired — a newer query is in flight. */
        return;
      }
      if (error instanceof ApiError && error.isAuthError) {
        get().logout();
        set({ searchResults: [], searchLoading: false, searchError: null });
        return;
      }
      const detail =
        error instanceof ApiError
          ? error.status === 404
            ? "Поиск ещё не подключён на сервере (/api/search недоступен)."
            : error.message
          : "Не удалось выполнить поиск.";
      set({ searchResults: [], searchLoading: false, searchError: detail });
    } finally {
      if (searchAbort === controller) searchAbort = null;
    }
  },

  clearSearch: () => {
    cancelPendingSearch();
    set({ searchQuery: "", searchResults: [], searchLoading: false, searchError: null });
  },

  setSearchSender: (sender) => {
    if (get().searchSender === sender) return;
    set({ searchSender: sender });
    if (get().searchQuery.trim()) void get().runSearch();
  },

  /**
   * CONTRACTS_V5 §6.3 — "click a result -> the chat opens and the message flashes".
   *
   * The order matters: the overlay closes first (it traps focus), then the
   * context window is stitched in *before* the chat is selected, and only then is
   * `selectChat(..., { preserveContext: true })` allowed to switch the pane — so
   * the timeline already contains `messageId` when `ChatWindow` mounts and its
   * `#msg-<id>` / `flash-target` effect can find the node.
   */
  openMessageInChat: async (chatId, messageId) => {
    get().closeSearch();

    /* Already on screen -> a plain flash is enough (and instant). */
    const cached = (get().messagesByChat[chatId] ?? []).some((message) => message.id === messageId);
    const target = get().selectedChatId;

    if (!cached && get().mode === "live") {
      try {
        await loadContextAround(chatId, messageId);
      } catch (error) {
        /* Still worth landing in the chat: the operator can scroll by hand. */
        get().pushToast(
          "warning",
          error instanceof ApiError
            ? `Не удалось загрузить контекст: ${error.message}`
            : "Не удалось загрузить контекст — сообщение может быть выше на странице.",
        );
      }
    }

    if (target !== chatId) {
      await get().selectChat(chatId, { preserveContext: true });
    }

    get().jumpToMessage(messageId);
    const found = await waitForMessageNode(messageId);
    if (!found) {
      get().pushToast("warning", "Сообщение вне загруженного окна — прокрутите историю вверх.");
    }
  },


  sendMessage: async () => {
    const { selectedChatId, draft, replyTargetId, operator, mode, sending } = get();
    if (selectedChatId === null || sending) return;
    const text = draft.trim();
    if (!text) return;
    if (!operator) return;

    const chatId = selectedChatId;
    set({ sending: true, draft: "", replyTargetId: null });
    get().endTyping(chatId);

    const nonce = uuid();
    const optimistic: Message = {
      id: -Date.now(),
      chat_id: chatId,
      tg_msg_id: null,
      sender_type: "OPERATOR",
      sender_operator_id: operator.id,
      sender_name: operator.display_name,
      sender_tg_id: null,
      text,
      reply_to_id: replyTargetId,
      reply_to_tg_msg_id:
        get().messagesByChat[chatId]?.find((message) => message.id === replyTargetId)?.tg_msg_id ?? null,
      phone: null,
      is_deleted_in_tg: false,
      deleted_at: null,
      sla_incident_id: null,
      delivery_status: "pending",
      is_outgoing: true,
      created_at: new Date().toISOString(),
    };

    set((state) => ({
      messagesByChat: {
        ...state.messagesByChat,
        [chatId]: [...(state.messagesByChat[chatId] ?? []), optimistic],
      },
      chats: state.chats.map((chat) =>
        chat.id === chatId
          ? {
              ...chat,
              unread_count: 0,
              last_message: {
                id: optimistic.id,
                text,
                sender_name: operator.display_name,
                is_outgoing: true,
                created_at: optimistic.created_at,
              },
            }
          : chat,
      ),
    }));

    if (mode === "mock") {
      window.setTimeout(() => {
        set((state) => ({
          messagesByChat: {
            ...state.messagesByChat,
            [chatId]: (state.messagesByChat[chatId] ?? []).map((message) =>
              message.id === optimistic.id ? { ...message, delivery_status: "sent" } : message,
            ),
          },
          sending: false,
        }));
      }, 350);
      return;
    }

    try {
      const created = await api.sendMessage(chatId, {
        text,
        reply_to_msg_id: replyTargetId,
        client_nonce: nonce,
      });
      set((state) => ({
        messagesByChat: {
          ...state.messagesByChat,
          [chatId]: (state.messagesByChat[chatId] ?? []).map((message) =>
            message.id === optimistic.id ? created : message,
          ),
        },
        slaStats: get().slaStats,
      }));
      if (replyTargetId !== null) {
        void api.slaStats().then((stats) => set({ slaStats: stats })).catch(() => undefined);
      }
    } catch (error) {
      if (error instanceof ApiError && error.isConflict) {
        const payload = error.payload as { locked_by?: string } | null;
        get().pushToast("warning", `Locked: ${payload?.locked_by ?? "another operator"} is replying to this chat.`);
        set((state) => ({
          messagesByChat: {
            ...state.messagesByChat,
            [chatId]: (state.messagesByChat[chatId] ?? []).filter((message) => message.id !== optimistic.id),
          },
          draft: text,
        }));
      } else {
        set((state) => ({
          messagesByChat: {
            ...state.messagesByChat,
            [chatId]: (state.messagesByChat[chatId] ?? []).map((message) =>
              message.id === optimistic.id ? { ...message, delivery_status: "failed" } : message,
            ),
          },
        }));
        get().pushToast("error", error instanceof ApiError ? error.message : "Message failed");
      }
    } finally {
      set({ sending: false });
    }
  },

  /* ---------------------------------------------------------------- media */

  /**
   * CONTRACTS_V2 §3.1 — `POST /api/chats/{chat_id}/attachments` (multipart).
   *
   * One request produces exactly one CRM message: the composer clears the
   * caption and the picked files only after the backend confirmed the upload,
   * so a failed send never loses the operator's selection. `MESSAGE_MEDIA` and
   * `NEW_MESSAGE` arrive over WS and seed the timeline; the response is merged
   * as well so a socket hiccup cannot leave the message invisible.
   */
  uploadFile: async (chatId, files, text, replyToId) => {
    const { operator, mode, sending } = get();
    if (sending || !operator) return false;
    const picked = files.filter((file) => file.size > 0);
    if (picked.length === 0) return false;

    const caption = (text ?? "").trim().slice(0, MAX_MEDIA_CAPTION_LENGTH);
    const replyTarget = replyToId ?? get().replyTargetId;
    const quoted =
      replyTarget === null ? undefined : get().messagesByChat[chatId]?.find((item) => item.id === replyTarget);

    set({ sending: true });
    get().endTyping(chatId);

    if (mode === "mock") {
      const created = buildLocalMediaMessage({
        chatId,
        operator,
        caption,
        replyToId: replyTarget ?? null,
        replyToTgMsgId: quoted?.tg_msg_id ?? null,
        files: picked,
      });
      set((state) => ({
        messagesByChat: { ...state.messagesByChat, [chatId]: mergeMessage(state.messagesByChat[chatId] ?? [], created) },
        draft: "",
        replyTargetId: null,
        sending: false,
        chats: state.chats.map((chat) =>
          chat.id === chatId
            ? {
                ...chat,
                ...computeUiChat(
                  {
                    ...chat,
                    last_message: {
                      id: created.id,
                      text: caption || `[${picked.length} file(s)]`,
                      sender_name: operator.display_name,
                      is_outgoing: true,
                      created_at: created.created_at,
                    },
                  },
                  Date.now(),
                ),
              }
            : chat,
        ),
      }));
      get().pushToast("info", "Mock mode: файл не отправлен на сервер (демо-режим).");
      return true;
    }

    try {
      const created = await api.uploadAttachment(chatId, picked, caption || undefined, replyTarget ?? undefined);
      const primary = created[created.length - 1];
      if (!primary) {
        get().pushToast("error", "Сервер не вернул созданное сообщение.");
        return false;
      }
      set((state) => ({
        messagesByChat: {
          ...state.messagesByChat,
          [chatId]: mergeMessage(state.messagesByChat[chatId] ?? [], primary),
        },
        draft: "",
        replyTargetId: null,
      }));
      if (replyTarget !== null) {
        void api.slaStats().then((stats) => set({ slaStats: stats })).catch(() => undefined);
      }
      get().pushToast("success", `Отправлено: ${picked.length} файл(ов).`);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError) {
        get().logout();
        return false;
      }
      if (error instanceof ApiError && error.isConflict) {
        const payload = error.payload as { locked_by?: string } | null;
        get().pushToast("warning", `Чат заблокирован: ${payload?.locked_by ?? "другой оператор"} отвечает.`);
      } else {
        get().pushToast(
          "error",
          error instanceof ApiError ? error.message : "Не удалось загрузить файл. Попробуйте ещё раз.",
        );
      }
      return false;
    } finally {
      set({ sending: false });
    }
  },

  /* ----------------------------------------------------------- reactions */

  /**
   * CONTRACTS_V2 §3.2 — `PUT /api/messages/{id}/reaction`.
   *
   * Optimistic: the bubble paints the new counter on the very next frame, the
   * API confirms with the authoritative aggregate, and a failure restores the
   * exact pre-click slice plus a toast. Requests for one message are serialized
   * (see `enqueueReaction`) so rapid clicking cannot interleave.
   */
  putReaction: (messageId, emoji) => {
    const value = emoji.trim();
    if (!value || isOptimisticId(messageId)) return Promise.resolve();

    const snapshot = findMessageById(get().messagesByChat, messageId);
    if (!snapshot) return Promise.resolve();

    const chatId = snapshot.chat_id;
    const current = snapshot.message.reactions ?? EMPTY_REACTIONS;
    const before = reactionBaseline(messageId, current);
    /* Already holding this emoji → the click is a toggle-off, never a `PUT`. */
    if (before.find((item) => item.emoji === value)?.me) {
      return get().removeReaction(messageId);
    }

    set((state) => {
      const list = state.messagesByChat[chatId];
      if (!list) return {};
      const optimistic = reactionAfterPut(before, value);
      return {
        messagesByChat: {
          ...state.messagesByChat,
          [chatId]: list.map((message) =>
            message.id === messageId ? { ...message, reactions: optimistic } : message,
          ),
        },
      };
    });

    if (get().mode === "mock") {
      /* No server to confirm, so the optimistic paint *is* the confirmed state. */
      rememberConfirmed(messageId, reactionAfterPut(before, value));
      return Promise.resolve();
    }

    return enqueueReaction(messageId, async () => {
      try {
        const response = await api.putReaction(messageId, value);
        if (Array.isArray(response.items)) replaceMessageReactions(set, chatId, messageId, response.items);
        else rememberConfirmed(messageId, reactionAfterPut(before, value));
      } catch (error) {
        replaceMessageReactions(set, chatId, messageId, before);
        if (error instanceof ApiError && error.isAuthError) get().logout();
        else get().pushToast("error", error instanceof ApiError ? error.message : "Не удалось поставить реакцию.");
      }
    });
  },

  /**
   * CONTRACTS_V2 §3.2 — `DELETE /api/messages/{id}/reaction` (removes *every*
   * reaction the caller holds on that message). Same optimistic/rollback rules.
   */
  removeReaction: (messageId) => {
    if (isOptimisticId(messageId)) return Promise.resolve();

    const snapshot = findMessageById(get().messagesByChat, messageId);
    if (!snapshot) return Promise.resolve();

    const chatId = snapshot.chat_id;
    const current = snapshot.message.reactions ?? EMPTY_REACTIONS;
    const before = reactionBaseline(messageId, current);
    if (!before.some((item) => item.me)) return Promise.resolve();

    set((state) => {
      const list = state.messagesByChat[chatId];
      if (!list) return {};
      const optimistic = reactionAfterRemove(before);
      return {
        messagesByChat: {
          ...state.messagesByChat,
          [chatId]: list.map((message) =>
            message.id === messageId ? { ...message, reactions: optimistic } : message,
          ),
        },
      };
    });

    if (get().mode === "mock") {
      rememberConfirmed(messageId, reactionAfterRemove(before));
      return Promise.resolve();
    }

    return enqueueReaction(messageId, async () => {
      try {
        const response = await api.deleteReaction(messageId);
        if (Array.isArray(response.items)) replaceMessageReactions(set, chatId, messageId, response.items);
        else rememberConfirmed(messageId, reactionAfterRemove(before));
      } catch (error) {
        replaceMessageReactions(set, chatId, messageId, before);
        if (error instanceof ApiError && error.isAuthError) get().logout();
        else get().pushToast("error", error instanceof ApiError ? error.message : "Не удалось снять реакцию.");
      }
    });
  },

  /* --------------------------------------- CONTRACTS_V3 §2/§3.2 — editing */

  startEditing: (message) => {
    if (isOptimisticId(message.id)) {
      get().pushToast("warning", "Сообщение ещё отправляется — редактирование недоступно.");
      return;
    }
    if (message.is_deleted_in_tg) {
      get().pushToast("warning", "Сообщение удалено в Telegram — редактирование недоступно.");
      return;
    }
    /* One editor at a time: an edit target from another chat would write into the
       wrong timeline, and switching chats already clears the mode. */
    set({ editingMessageId: message.id, editingDraft: message.text });
  },

  cancelEditing: () => {
    if (get().editingMessageId === null && get().editingDraft === "") return;
    set({ editingMessageId: null, editingDraft: "" });
  },

  /**
   * CONTRACTS_V3 §2.3 — `PATCH /api/messages/{id}`.
   *
   * On success the message text and `edited_at` are patched in place. Edit mode is
   * only left on success: on any failure the operator keeps their text and the
   * banner (a lost draft would have to be retyped), and a toast explains why —
   * `409` for a message deleted in Telegram (§5.5), `403` for a missing right.
   */
  saveEdit: async (text) => {
    const { editingMessageId, messagesByChat, mode } = get();
    if (editingMessageId === null) return;
    if (isOptimisticId(editingMessageId)) {
      get().cancelEditing();
      return;
    }

    const next = text.trim();
    if (!next) {
      get().pushToast("warning", "Текст сообщения не может быть пустым.");
      return;
    }
    if (next.length > MAX_EDIT_LENGTH) {
      get().pushToast("warning", `Текст длиннее ${MAX_EDIT_LENGTH} символов — Telegram не примет правку.`);
      return;
    }

    const location = findMessageById(messagesByChat, editingMessageId);
    if (!location) {
      get().pushToast("error", "Сообщение не найдено в загруженной истории.");
      return;
    }

    /* Nothing changed → close the mode without a pointless round trip (a Telegram
       `edit_message` with identical text is rejected by the worker). */
    if (location.message.text === next) {
      get().cancelEditing();
      return;
    }

    const patch = (values: Partial<Message>): void => {
      set((state) => {
        const list = state.messagesByChat[location.chat_id];
        if (!list) return {};
        return {
          messagesByChat: {
            ...state.messagesByChat,
            [location.chat_id]: list.map((item) =>
              item.id === editingMessageId ? { ...item, ...values } : item,
            ),
          },
        };
      });
    };

    if (mode === "mock") {
      patch({ text: next, edited_at: new Date().toISOString(), delivery_status: "sent" });
      set({ editingMessageId: null, editingDraft: "" });
      get().pushToast("info", "Mock mode: правка не отправлена на сервер (демо-режим).");
      return;
    }

    try {
      const response = await api.editMessage(editingMessageId, next);
      const updated = response?.message;
      patch({
        ...(updated && typeof updated === "object" ? updated : {}),
        text: updated?.text ?? next,
        edited_at: updated?.edited_at ?? new Date().toISOString(),
      });
      set({ editingMessageId: null, editingDraft: "" });
      get().pushToast(
        "success",
        response?.edited_in_telegram === false
          ? "Сохранено в CRM — Telegram не подтвердил правку, повторите позже."
          : "Сообщение изменено.",
      );
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError) {
        get().logout();
        return;
      }
      if (error instanceof ApiError && error.isConflict) {
        get().pushToast("warning", `Правка отклонена: ${error.message}`);
      } else {
        get().pushToast("error", error instanceof ApiError ? error.message : "Не удалось изменить сообщение.");
      }
    }
  },

  /* ----------------------------------------- CONTRACTS_V3 §2 — edit history */

  toggleEditHistory: (messageId) => {
    const open = get().editHistoryOpen[messageId] === true;
    set((state) => ({ editHistoryOpen: { ...state.editHistoryOpen, [messageId]: !open } }));
    /* First open loads the list; a reopen reuses what is already cached. */
    if (!open && get().editHistory[messageId] === undefined && !get().editHistoryLoading[messageId]) {
      void get().loadEditHistory(messageId);
    }
  },

  loadEditHistory: async (messageId) => {
    if (get().mode === "mock") {
      set((state) => ({
        editHistory: state.editHistory[messageId] ? state.editHistory : { ...state.editHistory, [messageId]: EMPTY_EDITS },
        editHistoryLoading: { ...state.editHistoryLoading, [messageId]: false },
        editHistoryError: { ...state.editHistoryError, [messageId]: null },
      }));
      return;
    }

    set((state) => ({
      editHistoryLoading: { ...state.editHistoryLoading, [messageId]: true },
      editHistoryError: { ...state.editHistoryError, [messageId]: null },
    }));

    try {
      const response = await api.getMessageEdits(messageId);
      const items = asMessageEdits(response?.items);
      set((state) => ({
        editHistory: { ...state.editHistory, [messageId]: items ?? EMPTY_EDITS },
        editHistoryLoading: { ...state.editHistoryLoading, [messageId]: false },
        editHistoryError: { ...state.editHistoryError, [messageId]: null },
      }));
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "Не удалось загрузить историю правок.";
      set((state) => ({
        editHistoryLoading: { ...state.editHistoryLoading, [messageId]: false },
        editHistoryError: { ...state.editHistoryError, [messageId]: message },
      }));
    }
  },

  /* ---------------------------------------- CONTRACTS_V3 §1 — forward dialog */

  openForwardDialog: (message) => {
    if (isOptimisticId(message.id)) {
      get().pushToast("warning", "Сообщение ещё отправляется — пересылка недоступна.");
      return;
    }
    if (message.is_deleted_in_tg) {
      get().pushToast("warning", "Сообщение удалено в Telegram — пересылка недоступна.");
      return;
    }
    set({ forwardSource: message, forwardError: null, forwardSubmitting: false });
  },

  closeForwardDialog: () => {
    if (get().forwardSubmitting) return;
    set({ forwardSource: null, forwardError: null, forwardSubmitting: false });
  },

  /**
   * CONTRACTS_V3 §1.2 — `POST /api/messages/{id}/forward`.
   *
   * The copy is created by the backend in the *target* chat as our own message
   * (`forwarded_from = NULL`, §5.1) and the call answers with that `MessageOut`.
   * The dialog stays open with `forwardError` on failure so the operator can
   * retry or pick another chat; `409` means the target is locked by another
   * operator's typing lock, `422` means the target is the source chat.
   */
  forwardMessage: async (targetChatId, comment) => {
    const { forwardSource, forwardSubmitting, mode } = get();
    if (!forwardSource || forwardSubmitting) return;

    if (targetChatId === forwardSource.chat_id) {
      set({ forwardError: "Исходный и целевой чат совпадают — выберите другой чат." });
      return;
    }

    if (mode === "mock") {
      get().pushToast("info", "Mock mode: пересылка не отправлена на сервер (демо-режим).");
      set({ forwardSource: null, forwardError: null, forwardSubmitting: false });
      return;
    }

    set({ forwardSubmitting: true, forwardError: null });
    try {
      const created = await api.forwardMessage(forwardSource.id, targetChatId, comment);
      if (created && typeof created === "object") {
        set((state) => ({ messagesByChat: mergeIntoLoadedChat(state.messagesByChat, targetChatId, created) }));
      }
      set({ forwardSource: null, forwardSubmitting: false, forwardError: null });
      get().pushToast("success", "Сообщение переслано.");
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError) {
        get().logout();
        return;
      }
      const detail = error instanceof ApiError ? error.message : "Не удалось переслать сообщение.";
      set({ forwardSubmitting: false, forwardError: detail });
      get().pushToast("error", detail);
    }
  },

  /* ------------------------------------------ CONTRACTS_V5 §1 — deletion */

  /**
   * §1.3 — `DELETE /api/messages/{id}`.
   *
   * The row is never removed (§1.2, invariant §8.1): the server answers with the
   * full updated `MessageOut` and the timeline entry is replaced **in place**, so
   * the bubble turns into a grey trace without a re-fetch and without losing the
   * operator's scroll position. Falling back to the local fields keeps the UI
   * honest when the payload somehow omits the flags.
   *
   * A Telegram mirror failure (`telegram.delivered === false`) is deliberately not
   * an error — the CRM commit already happened (§8.3) — but it is surfaced as a
   * warning toast, because the operator should know the client may still see it.
   */
  deleteMessage: async (messageId) => {
    const { messagesByChat, mode } = get();

    if (isOptimisticId(messageId)) {
      get().pushToast("warning", "Сообщение ещё отправляется — удаление недоступно.");
      return false;
    }

    const location = findMessageById(messagesByChat, messageId);
    if (location?.message.is_deleted_in_tg) {
      get().pushToast("warning", "Сообщение уже удалено.");
      return false;
    }

    if (mode === "mock") {
      const deletedAt = new Date().toISOString();
      if (location) {
        set((state) => ({
          messagesByChat: {
            ...state.messagesByChat,
            [location.chat_id]: state.messagesByChat[location.chat_id]?.map((message) =>
              message.id === messageId
                ? { ...message, is_deleted_in_tg: true, deleted_at: deletedAt, deleted_source: "CRM" }
                : message,
            ) ?? [],
          },
        }));
      }
      get().pushToast("info", "Mock mode: удаление не отправлено на сервер (демо-режим).");
      return true;
    }

    try {
      const response = await api.deleteMessage(messageId);
      const updated = response?.message;
      const fallbackChatId = location?.chat_id ?? Number(response?.chat_id);

      /** Replace the message object in place — order and identity of neighbours stay. */
      const replaceInPlace = (chatId: number, replacement: Message): void => {
        set((state) => {
          const list = state.messagesByChat[chatId];
          if (!list) return {};
          let touched = false;
          const next = list.map((message) => {
            if (message.id !== messageId) return message;
            touched = true;
            return replacement;
          });
          return touched ? { messagesByChat: { ...state.messagesByChat, [chatId]: next } } : {};
        });
      };

      if (updated && typeof updated === "object") {
        const chatId = Number.isFinite(Number(updated.chat_id)) ? Number(updated.chat_id) : fallbackChatId;
        if (Number.isFinite(chatId)) {
          const source = location?.message;
          const merged: Message = {
            ...(source ?? {}),
            ...updated,
            /* The response is authoritative, but never let a partial payload undo a
               deletion the server just confirmed. */
            is_deleted_in_tg: updated.is_deleted_in_tg !== false,
            deleted_at: updated.deleted_at ?? response?.deleted_at ?? new Date().toISOString(),
            deleted_source: updated.deleted_source ?? response?.deleted_source ?? "CRM",
          };
          replaceInPlace(chatId, merged);
        }
      } else if (Number.isFinite(fallbackChatId)) {
        set((state) => {
          const list = state.messagesByChat[fallbackChatId];
          if (!list) return {};
          return {
            messagesByChat: {
              ...state.messagesByChat,
              [fallbackChatId]: list.map((message) =>
                message.id === messageId
                  ? {
                      ...message,
                      is_deleted_in_tg: true,
                      deleted_at: response?.deleted_at ?? new Date().toISOString(),
                      deleted_source: "CRM",
                    }
                  : message,
              ),
            },
          };
        });
      }

      const telegram = response?.telegram;
      if (telegram && telegram.requested && telegram.delivered === false) {
        get().pushToast(
          "warning",
          telegram.error
            ? `В CRM удалено, но Telegram не подтвердил удаление: ${telegram.error}`
            : "В CRM удалено, но Telegram не подтвердил удаление.",
        );
      } else if (telegram && telegram.requested === false) {
        get().pushToast("success", "Сообщение удалено в CRM (в Telegram его не было).");
      } else {
        get().pushToast("success", "Сообщение удалено.");
      }
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError && error.status === 401) {
        get().logout();
        return false;
      }
      get().pushToast("error", deleteMessageErrorMessage(error));
      return false;
    }
  },

  /* -------------------------------- CONTRACTS_V5 §5 — editable quick replies */

  /**
   * §5.2 — `GET /api/quick-replies`.
   *
   * Called by both the composer (on mount) and the editor (on open). Without
   * `force` a non-empty list is reused, so opening the editor does not flicker
   * the composer's chips; `force` re-reads after a failure or a WS reconnect.
   */
  loadQuickReplies: async (options) => {
    if (get().quickRepliesLoading) return;

    if (get().mode === "mock") {
      set({ quickRepliesLoading: false, quickRepliesError: null });
      return;
    }

    if (!options?.force && get().quickReplies.length > 0) return;

    set({ quickRepliesLoading: true, quickRepliesError: null });
    try {
      /* `?all=true` (retired replies included) is administrator-only on the backend,
         so the variant is chosen *before* the call instead of probing and absorbing a
         `403`: a refused request is a failed request in the network panel and in the
         console, and the composer only ever renders active replies anyway. The editor
         asks for the full list because it must be able to reactivate a retired one. */
      const operator = get().operator;
      const includeInactive = operator?.role === "ADMIN";
      const response = await api.listQuickReplies(includeInactive);
      const items = asQuickReplies(response?.items) ?? [];
      set((state) => ({
        quickRepliesLoading: false,
        quickRepliesError: null,
        /* Never clobber a fresher optimistic list with a stale response. */
        quickReplies: state.quickReplyMutating ? state.quickReplies : items,
      }));
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError && error.status === 401) {
        get().logout();
        return;
      }
      const detail = quickReplyErrorMessage(error, "Не удалось загрузить быстрые ответы.");
      set({ quickRepliesLoading: false, quickRepliesError: detail });
    }
  },

  /**
   * §5.2 — `POST /api/quick-replies`.
   *
   * Optimistic: the row is appended immediately under a negative id so the editor
   * and the composer chip list react without waiting for the round trip. On any
   * failure the slice is restored to the exact pre-click snapshot (not a filtered
   * version of the optimistic list) and the reason is toasted.
   */
  createQuickReply: async (payload) => {
    const { quickReplies, quickReplyMutating, operator, mode } = get();
    if (quickReplyMutating) return null;

    const fields = {
      shortcut: payload.shortcut ?? "",
      title: payload.title ?? "",
      body: payload.body ?? "",
      category: payload.category ?? QUICK_REPLY_DEFAULT_CATEGORY,
    };
    const invalid = validateQuickReplyFields(fields, quickReplies, null);
    if (invalid) {
      get().pushToast("warning", invalid);
      set({ quickRepliesError: invalid });
      return null;
    }

    const now = new Date().toISOString();
    const draft: QuickReply = {
      id: nextOptimisticQuickReplyId(),
      shortcut: fields.shortcut.trim().toLowerCase(),
      title: fields.title.trim(),
      body: fields.body,
      category: fields.category.trim() || QUICK_REPLY_DEFAULT_CATEGORY,
      sort_order: payload.sort_order ?? (quickReplies.length === 0 ? 0 : Math.max(...quickReplies.map((item) => item.sort_order)) + 10),
      is_active: payload.is_active ?? true,
      created_by: operator?.display_name || operator?.username || null,
      created_at: now,
      updated_at: now,
    };

    if (mode === "mock") {
      set((state) => ({
        quickReplies: sortQuickReplies([...state.quickReplies, draft]),
        quickRepliesError: null,
        quickReplyPendingId: null,
      }));
      get().pushToast("info", "Mock mode: быстрый ответ сохранён только локально (демо-режим).");
      return draft;
    }

    const snapshot = quickReplies;
    set({
      quickReplies: sortQuickReplies([...quickReplies, draft]),
      quickReplyMutating: true,
      quickReplyPendingId: draft.id,
      quickRepliesError: null,
    });

    try {
      const created = await api.createQuickReply({
        shortcut: draft.shortcut,
        title: draft.title,
        body: draft.body,
        category: draft.category,
        sort_order: draft.sort_order,
        is_active: draft.is_active,
      });
      set((state) => {
        /* Swap the optimistic row for the server one; a concurrent WS broadcast may
           already contain it, in which case the duplicate is dropped. */
        const withoutDraft = state.quickReplies.filter((item) => item.id !== draft.id && item.id !== created?.id);
        const next = created && typeof created === "object" ? [...withoutDraft, created] : withoutDraft;
        return {
          quickReplies: sortQuickReplies(next),
          quickReplyMutating: false,
          quickReplyPendingId: null,
          quickRepliesError: null,
        };
      });
      get().pushToast("success", `Быстрый ответ «${draft.title}» создан.`);
      return created ?? draft;
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError && error.status === 401) {
        get().logout();
        return null;
      }
      const detail = quickReplyErrorMessage(error, "Не удалось создать быстрый ответ.");
      set({ quickReplies: snapshot, quickReplyMutating: false, quickReplyPendingId: null, quickRepliesError: detail });
      get().pushToast("error", detail);
      return null;
    }
  },

  /**
   * §5.2 — `PATCH /api/quick-replies/{id}`.
   *
   * Only the keys the caller supplies are sent (the backend rejects an empty
   * patch with `422`), the local row is patched optimistically, and a failure
   * restores the pre-click snapshot.
   */
  updateQuickReply: async (id, payload) => {
    const { quickReplies, quickReplyMutating, mode } = get();
    if (quickReplyMutating) return null;

    const current = quickReplies.find((item) => item.id === id);
    if (!current) {
      get().pushToast("error", "Быстрый ответ не найден — список мог обновиться. Откройте редактор заново.");
      return null;
    }

    const fields = {
      shortcut: payload.shortcut ?? current.shortcut,
      title: payload.title ?? current.title,
      body: payload.body ?? current.body,
      category: payload.category ?? current.category,
    };
    const invalid = validateQuickReplyFields(fields, quickReplies, id);
    if (invalid) {
      get().pushToast("warning", invalid);
      set({ quickRepliesError: invalid });
      return null;
    }

    /* A `PATCH` with no field set is a backend `422`: bail out instead of sending it. */
    const changes: QuickReplyUpdatePayload = {};
    if (payload.shortcut !== undefined && fields.shortcut.trim().toLowerCase() !== current.shortcut) {
      changes.shortcut = fields.shortcut.trim().toLowerCase();
    }
    if (payload.title !== undefined && fields.title.trim() !== current.title) {
      changes.title = fields.title.trim();
    }
    if (payload.body !== undefined && fields.body !== current.body) {
      changes.body = fields.body;
    }
    if (payload.category !== undefined && fields.category.trim() !== current.category) {
      changes.category = fields.category.trim() || QUICK_REPLY_DEFAULT_CATEGORY;
    }
    if (payload.sort_order !== undefined && payload.sort_order !== current.sort_order) {
      changes.sort_order = payload.sort_order;
    }
    if (payload.is_active !== undefined && payload.is_active !== current.is_active) {
      changes.is_active = payload.is_active;
    }

    if (Object.keys(changes).length === 0) {
      /* Nothing to persist: report success so the editor closes without an error. */
      set({ quickRepliesError: null });
      return current;
    }

    const optimistic: QuickReply = {
      ...current,
      ...changes,
      updated_at: new Date().toISOString(),
    };

    if (mode === "mock") {
      set((state) => ({
        quickReplies: sortQuickReplies(state.quickReplies.map((item) => (item.id === id ? optimistic : item))),
        quickRepliesError: null,
        quickReplyPendingId: null,
      }));
      get().pushToast("info", "Mock mode: изменения сохранены только локально (демо-режим).");
      return optimistic;
    }

    const snapshot = quickReplies;
    set({
      quickReplies: sortQuickReplies(quickReplies.map((item) => (item.id === id ? optimistic : item))),
      quickReplyMutating: true,
      quickReplyPendingId: id,
      quickRepliesError: null,
    });

    try {
      const updated = await api.updateQuickReply(id, changes);
      set((state) => ({
        quickReplies: sortQuickReplies(
          state.quickReplies.map((item) =>
            item.id === id ? (updated && typeof updated === "object" ? updated : optimistic) : item,
          ),
        ),
        quickReplyMutating: false,
        quickReplyPendingId: null,
        quickRepliesError: null,
      }));
      get().pushToast("success", "Быстрый ответ сохранён.");
      return updated ?? optimistic;
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError && error.status === 401) {
        get().logout();
        return null;
      }
      const detail = quickReplyErrorMessage(error, "Не удалось сохранить быстрый ответ.");
      set({ quickReplies: snapshot, quickReplyMutating: false, quickReplyPendingId: null, quickRepliesError: detail });
      get().pushToast("error", detail);
      return null;
    }
  },

  /**
   * §5.2 — `DELETE /api/quick-replies/{id}`.
   *
   * Optimistic removal with a full-snapshot rollback: the removed row is restored
   * at its previous position on failure, so the list order never drifts.
   */
  deleteQuickReply: async (id) => {
    const { quickReplies, quickReplyMutating, mode } = get();
    if (quickReplyMutating) return false;

    const current = quickReplies.find((item) => item.id === id);
    if (!current) {
      set({ quickRepliesError: "Быстрый ответ не найден — список мог обновиться." });
      return false;
    }

    if (mode === "mock") {
      set((state) => ({
        quickReplies: state.quickReplies.filter((item) => item.id !== id),
        quickRepliesError: null,
        quickReplyPendingId: null,
      }));
      get().pushToast("info", "Mock mode: удалено только локально (демо-режим).");
      return true;
    }

    const snapshot = quickReplies;
    set({
      quickReplies: quickReplies.filter((item) => item.id !== id),
      quickReplyMutating: true,
      quickReplyPendingId: id,
      quickRepliesError: null,
    });

    try {
      await api.deleteQuickReply(id);
      set({ quickReplyMutating: false, quickReplyPendingId: null, quickRepliesError: null });
      get().pushToast("success", `Быстрый ответ «${current.title}» удалён.`);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError && error.status === 401) {
        get().logout();
        return false;
      }
      /* `404` means the row is already gone: the optimistic removal was right. */
      if (error instanceof ApiError && error.status === 404) {
        set({ quickReplyMutating: false, quickReplyPendingId: null, quickRepliesError: null });
        get().pushToast("warning", "Быстрый ответ уже удалён.");
        return true;
      }
      const detail = quickReplyErrorMessage(error, "Не удалось удалить быстрый ответ.");
      set({ quickReplies: snapshot, quickReplyMutating: false, quickReplyPendingId: null, quickRepliesError: detail });
      get().pushToast("error", detail);
      return false;
    }
  },

  /* ----------------------------------------------------- typing collision */

  beginTyping: (chatId) => {
    const { typingLocks, operator, mode, myLockChatId } = get();
    const lock = typingLocks[chatId];
    if (lock && operator && lock.operator_id !== operator.id && lock.remaining_ms > 0) return;

    // Only touch the store when ownership actually changes: a no-op ``set`` still
    // notifies every subscriber, which re-renders the controlled textarea mid-keystroke
    // and drops characters/caret position.
    if (myLockChatId !== chatId) set({ myLockChatId: chatId });
    if (mode === "mock") return;
    wsClient?.lockChat(chatId);
    void api.sendTyping(chatId, "start").catch(() => undefined);
  },

  endTyping: (chatId) => {
    const { myLockChatId, mode } = get();
    if (myLockChatId !== chatId) return;
    set({ myLockChatId: null });
    if (mode === "mock") return;
    wsClient?.unlockChat(chatId);
    void api.sendTyping(chatId, "stop").catch(() => undefined);
  },

  /* ------------------------------------------------------------- ws inflow */

  handleWsEnvelope: (envelope) => {
    const { type, data } = envelope;
    const operator = get().operator;

    switch (type) {
      case "NEW_MESSAGE": {
        const payload = data as WsNewMessageData;
        const message: Message = payload;
        set((state) => {
          const existing = state.messagesByChat[message.chat_id] ?? [];
          /* Drop the optimistic twin of this message — matched by its idempotency
             key or by id. The predicate must be `id !== incoming && nonce === incoming`,
             not `id !== incoming && nonce !== undefined`: the latter also discards
             *other* pending local messages, which then flicker out and back. */
          const filtered = existing.filter(
            (item) =>
              item.id !== message.id &&
              !(message.client_nonce !== undefined && message.client_nonce !== null && item.client_nonce === message.client_nonce),
          );
          const isOpen = state.selectedChatId === message.chat_id;
          const nextList = mergeMessage(filtered, message);
          if (!isOptimisticId(message.id) && Array.isArray(message.reactions)) {
            rememberConfirmed(message.id, message.reactions);
          }
          return {
            messagesByChat: { ...state.messagesByChat, [message.chat_id]: nextList },
            chats: sortChats(
              state.chats.map((chat) =>
                chat.id === message.chat_id
                  ? {
                      ...chat,
                      unread_count: isOpen || message.is_outgoing ? chat.unread_count : chat.unread_count + 1,
                      last_message: {
                        id: message.id,
                        text: message.text,
                        sender_name: message.sender_name,
                        is_outgoing: message.is_outgoing,
                        created_at: message.created_at,
                      },
                    }
                  : chat,
              ),
            ),
          };
        });
        if (payload.chat) {
          set((state) => ({
            chats: sortChats(
              state.chats.map((chat) =>
                chat.id === payload.chat?.id
                  ? { ...computeUiChat({ ...chat, ...payload.chat }, Date.now()), unread_count: chat.unread_count }
                  : chat,
              ),
            ),
          }));
        }
        break;
      }

      case "MESSAGE_DELETED": {
        const payload = data as WsMessageDeletedData;
        /* CONTRACTS_V5 §1.2 — `deleted_source` arrives with the frame (the CHECK
           constraint guarantees it is non-null once the flag is set). It defaults to
           `TELEGRAM` here: this inbound event is the Telegram-side deletion path. */
        const source = payload.message?.deleted_source ?? null;
        set((state) => ({
          messagesByChat: {
            ...state.messagesByChat,
            [payload.chat_id]: (state.messagesByChat[payload.chat_id] ?? []).map((message) =>
              message.id === payload.message_id
                ? {
                    ...message,
                    ...(payload.message ?? {}),
                    is_deleted_in_tg: true,
                    deleted_at: payload.deleted_at,
                    deleted_source: source ?? message.deleted_source ?? "TELEGRAM",
                    text: payload.message?.text ?? message.text,
                  }
                : message,
            ),
          },
        }));
        break;
      }

      case "MESSAGE_EDITED": {
        /* CONTRACTS_V3 §2.6 — `{message_id, chat_id, text, edited_at, source, edit}`.
           `edit` is a `MessageEditOut` for Telegram edits and `null` for our own
           CRM edits (§2.1: CRM edits leave no history). */
        const payload = data as WsMessageEditedData;
        const messageId = Number(payload?.message_id);
        if (!payload || !Number.isFinite(messageId)) break;
        const editedAt = typeof payload.edited_at === "string" && payload.edited_at ? payload.edited_at : new Date().toISOString();
        const nextText = typeof payload.text === "string" ? payload.text : "";
        const edit = asMessageEdit(payload.edit);
        set((state) => {
          const chatHint = Number(payload.chat_id);
          const location = findMessageById(
            state.messagesByChat,
            messageId,
            Number.isFinite(chatHint) ? chatHint : undefined,
          );
          /* The history is worth recording even when the message is not loaded
             (another chat, older page) — the block appears once it is opened. */
          const editHistory = edit ? withEdit(state.editHistory, edit) : state.editHistory;
          if (!location) return edit ? { editHistory } : {};
          const list = state.messagesByChat[location.chat_id];
          if (!list) return edit ? { editHistory } : {};
          return {
            ...(edit ? { editHistory } : {}),
            messagesByChat: {
              ...state.messagesByChat,
              [location.chat_id]: list.map((message) =>
                message.id === messageId ? { ...message, text: nextText, edited_at: editedAt } : message,
              ),
            },
          };
        });
        break;
      }

      case "MESSAGE_FORWARDED": {
        /* CONTRACTS_V3 §2.6 — `{source_message_id, source_chat_id, message, chat}`.
           Only the *target* timeline is touched: the copy is a brand-new message
           in `data.chat.id`. `forwarded_from` is deliberately `NULL` (§5.1). */
        const payload = data as WsMessageForwardedData;
        const forwarded = payload?.message;
        if (!payload || !forwarded || typeof forwarded !== "object") break;
        const targetChatId = Number(forwarded.chat_id);
        if (!Number.isFinite(targetChatId)) break;
        set((state) => {
          const merged = mergeIntoLoadedChat(state.messagesByChat, targetChatId, forwarded);
          const chats = payload.chat
            ? sortChats(
                state.chats.map((chat) =>
                  chat.id === payload.chat?.id
                    ? { ...computeUiChat({ ...chat, ...payload.chat }, Date.now()), unread_count: chat.unread_count }
                    : chat,
                ),
              )
            : state.chats;
          /* Keep the object identity when neither side changed — a no-op `set` would
             still re-render every subscriber of the timeline. */
          if (merged === state.messagesByChat && chats === state.chats) return {};
          return { messagesByChat: merged, chats };
        });
        break;
      }

      case "MESSAGE_STATUS": {
        const payload = data as WsMessageStatusData;
        set((state) => ({
          messagesByChat: {
            ...state.messagesByChat,
            [payload.chat_id]: (state.messagesByChat[payload.chat_id] ?? []).map((message) =>
              message.id === payload.message_id
                ? { ...message, delivery_status: payload.delivery_status, tg_msg_id: payload.tg_msg_id }
                : message,
            ),
          },
        }));
        break;
      }

      /* ------------------------------------------- CONTRACTS_V2 §4 — media */

      case "MESSAGE_MEDIA": {
        const payload = data as WsMessageMediaData;
        const attachments = asAttachments(payload?.attachments);
        if (!payload || attachments === null) break;
        const messageId = Number(payload.message_id);
        if (!Number.isFinite(messageId)) break;
        set((state) => {
          const location = findMessageById(state.messagesByChat, messageId, Number(payload.chat_id) || undefined);
          if (!location) return {};
          const list = state.messagesByChat[location.chat_id];
          if (!list) return {};
          return {
            messagesByChat: {
              ...state.messagesByChat,
              [location.chat_id]: list.map((message) =>
                message.id === messageId
                  ? {
                      ...message,
                      attachments,
                      kind: message.kind && message.kind !== "TEXT" ? message.kind : kindOfAttachments(attachments),
                    }
                  : message,
              ),
            },
          };
        });
        break;
      }

      /* --------------------------------------- CONTRACTS_V2 §4 — reactions */

      case "MESSAGE_REACTION": {
        // Aggregated counter from the server: `{message_id, chat_id, emoji, count, operator_id}`.
        // It is applied verbatim (never incremented locally) so two tabs of the same
        // operator, or a Telegram-side reaction, cannot skew the number.
        const payload = data as WsMessageReactionData;
        applyReactionFrame(set, payload, "upsert");
        break;
      }

      case "MESSAGE_REACTION_REMOVED": {
        const payload = data as WsMessageReactionData;
        applyReactionFrame(set, payload, "remove");
        break;
      }

      case "QUICK_REPLIES_UPDATED": {
        /* CONTRACTS_V5 §5.3 — `{items: QuickReplyOut[]}`, fanned out to every
           operator after any create/update/delete, so an editor open in one tab
           (or on another machine) refreshes without a reload. The payload is the
           whole authoritative list: replace, never merge, so a deleted reply
           actually disappears. */
        const payload = data as WsQuickRepliesUpdatedData;
        const items = asQuickReplies(payload?.items);
        if (items === null) break;
        set((state) => {
          /* An in-flight local mutation will reconcile with its own response; a
             frame that predates the click must not resurrect a deleted row. */
          if (state.quickReplyMutating) return {};
          return sameQuickReplies(state.quickReplies, items)
            ? {}
            : { quickReplies: items, quickRepliesError: null };
        });
        break;
      }

      case "SLA_UPDATE": {
        const payload = data as WsSlaUpdateData;
        const now = Date.now();
        set((state) => ({
          chats: state.chats.map((chat) =>
            chat.id === payload.chat_id
              ? {
                  ...chat,
                  sla: {
                    status: payload.status,
                    deadline_at: payload.deadline_at,
                    remaining_seconds: payload.remaining_seconds,
                    is_overdue: payload.is_overdue,
                  },
                  sla_remaining_seconds: secondsUntil(payload.deadline_at, now),
                  sla_local: true,
                  assigned_operator_id: payload.assigned_operator_id ?? chat.assigned_operator_id,
                  assigned_operator_name: payload.assigned_operator_name ?? chat.assigned_operator_name,
                }
              : chat,
          ),
        }));
        break;
      }

      case "SLA_RESOLVED": {
        const payload = data as WsSlaResolvedData;
        set((state) => ({
          chats: state.chats.map((chat) =>
            chat.id === payload.chat_id ? { ...chat, sla: null, sla_remaining_seconds: null } : chat,
          ),
          slaStats: payload.stats ? payload.stats : state.slaStats,
        }));
        if (payload.operator_id === operator?.id) {
          get().pushToast("success", `SLA resolved in ${Math.max(1, Math.round(payload.response_time_seconds))}s.`);
        }
        break;
      }

      case "TYPING_LOCK": {
        const payload = data as WsTypingLockData;
        // A lock event without a holder is either a command acknowledgement or a
        // malformed frame — treating it as a lock would freeze the composer for the
        // operator who is actually typing.
        if (payload.operator_id === null || payload.operator_id === undefined) break;
        if (operator && payload.operator_id === operator.id) break;
        // Never fall back to a fixed 15 s: a malformed ``until`` would then freeze the
        // input with no way to recover. Clamp to the server TTL instead, and ignore a
        // frame whose deadline has already passed.
        const seconds = secondsUntil(payload.until);
        const remainingMs = Math.max(0, Math.min(seconds ?? TYPING_LOCK_TTL_SECONDS, TYPING_LOCK_TTL_SECONDS) * 1000);
        if (remainingMs <= 0) break;
        set((state) => ({
          typingLocks: {
            ...state.typingLocks,
            [payload.chat_id]: {
              chat_id: payload.chat_id,
              operator_id: payload.operator_id,
              operator_name: payload.operator_name ?? "another operator",
              until: payload.until,
              remaining_ms: remainingMs,
            },
          },
        }));
        break;
      }

      case "TYPING_UNLOCK": {
        const payload = data as WsTypingUnlockData;
        set((state) => {
          const current = state.typingLocks[payload.chat_id];
          // ``operator_id === null`` means a forced release (the server sends it when a
          // socket disconnects or after a successful send), so it must clear the entry
          // regardless of who held it. A non-null id only clears its own lock.
          const ownerMatches =
            payload.operator_id === null ||
            payload.operator_id === undefined ||
            current?.operator_id === payload.operator_id;
          if (!current || !ownerMatches) return {};
          const next = { ...state.typingLocks };
          delete next[payload.chat_id];
          return { typingLocks: next };
        });
        break;
      }

      case "CHAT_ASSIGNED": {
        const payload = data as WsChatAssignedData;
        set((state) => ({
          chats: state.chats.map((chat) =>
            chat.id === payload.chat_id
              ? {
                  ...chat,
                  assigned_operator_id: payload.assigned_operator_id,
                  assigned_operator_name: payload.assigned_operator_name,
                }
              : chat,
          ),
        }));
        break;
      }

      case "SHIFT_CHANGED": {
        const payload = data as WsShiftChangedData;
        set((state) => ({
          operators: state.operators.map((item) => (item.id === payload.id ? { ...item, ...payload } : item)),
          operator: state.operator && state.operator.id === payload.id ? { ...state.operator, ...payload } : state.operator,
        }));
        break;
      }

      case "OPERATOR_PRESENCE": {
        const payload = data as { operator_id: number; is_online_ws: boolean; shift_status: ShiftStatus };
        set((state) => ({
          operators: state.operators.map((item) =>
            item.id === payload.operator_id
              ? { ...item, is_online_ws: payload.is_online_ws, shift_status: payload.shift_status }
              : item,
          ),
        }));
        break;
      }

      case "CONNECTED":
        set({ connection: "open" });
        break;

      case "ERROR": {
        const payload = data as { detail?: string };
        get().pushToast("error", payload.detail ?? "Server error");
        break;
      }

      default:
        break;
    }
  },

  /* ------------------------------------------------------------- tx lookup */

  lookupTx: async (rawTxId, opts) => {
    const txId = rawTxId.trim().toUpperCase();
    if (!txId) return;
    const source = opts?.source ?? "manual";
    const panel: TxPanelKey = opts?.panel ?? "one";
    const stateKey = panel === "one" ? "txOne" : "txTwo";
    const panelCode: TxAdminPanel = panel === "one" ? "ADMIN_ONE" : "ADMIN_TWO";

    set({ [stateKey]: { ...get()[stateKey], tx_id: txId, loading: true, error: null, source } } as Partial<CRMState>);

    try {
      const result =
        get().mode === "mock" ? mockLookupTx(txId) : await api.lookupTx(txId, panelCode);
      set({
        txOne: { ...get().txOne, tx_id: txId, loading: false, error: null, result, source, looked_up_at: Date.now() },
        txTwo: { ...get().txTwo, tx_id: txId, loading: false, error: null, result, source, looked_up_at: Date.now() },
      } as Partial<CRMState>);
    } catch (error) {
      set({
        [stateKey]: {
          ...get()[stateKey],
          loading: false,
          error: error instanceof ApiError ? error.message : "Lookup failed",
          result: null,
        },
      } as Partial<CRMState>);
    }
  },

  insertTxIntoReply: (panel) => {
    const state = panel === "one" ? get().txOne : get().txTwo;
    if (!state.result) {
      get().pushToast("warning", "Look up a transaction first.");
      return;
    }
    get().appendToDraft(formatTxForReply(state.result));
    get().pushToast("success", `Inserted ${state.result.tx_id} summary into the reply.`);
  },

  clearTx: (panel) => {
    if (!panel || panel === "one") set({ txOne: emptyTxState() });
    if (!panel || panel === "two") set({ txTwo: emptyTxState() });
  },

  /* --------------------------------------------------------------- toasts */

  pushToast: (kind, message) => {
    const id = uuid();
    set((state) => ({ toasts: [...state.toasts.slice(-3), { id, kind, message, at: Date.now() }] }));
    window.setTimeout(() => get().dismissToast(id), kind === "error" ? 7000 : 4500);
  },

  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) })),

  /* ---------------------------------------------------------------- ticker */

  tick: () => {
    const now = Date.now();
    set((state) => {
      let changed = false;
      const chats = state.chats.map((chat) => {
        if (!chat.sla && chat.sla_remaining_seconds === null) return chat;
        const remaining = chat.sla ? secondsUntil(chat.sla.deadline_at, now) : null;
        if (remaining === chat.sla_remaining_seconds) return chat;
        changed = true;
        return { ...chat, sla_remaining_seconds: remaining };
      });

      const locks: Record<number, TypingLockView> = {};
      let locksChanged = false;
      for (const [key, lock] of Object.entries(state.typingLocks)) {
        const remainingMs = Math.max(0, (secondsUntil(lock.until, now) ?? 0) * 1000);
        if (remainingMs <= 0) {
          locksChanged = true;
          continue;
        }
        locks[Number(key)] = { ...lock, remaining_ms: remainingMs };
      }

      if (!changed && !locksChanged) return {};
      return locksChanged ? { chats, typingLocks: locks } : { chats };
    });
  },
}));

/* --------------------------------------------------------------- helpers */

type SetFn = (partial: Partial<CRMState> | ((state: CRMState) => Partial<CRMState>)) => void;
type GetFn = () => CRMState;

function activateMock(set: SetFn, get: GetFn): void {
  const dataset = createMockDataset();
  const operator = MOCK_CURRENT_OPERATOR;
  window.localStorage.setItem(OPERATOR_KEY, JSON.stringify(operator));
  set({
    mode: "mock",
    connection: "mock",
    operator,
    operators: MOCK_OPERATORS,
    slaStats: MOCK_SLA_STATS,
    chats: dataset.chats.map((chat) => computeUiChat(chat, Date.now())),
    messagesByChat: dataset.messagesByChat,
    selectedChatId: dataset.chats[0]?.id ?? null,
  });
  void get();
}

async function hydrate(set: SetFn, get: GetFn): Promise<void> {
  const [chatsResponse, statsResponse, operatorsResponse] = await Promise.allSettled([
    api.listChats("all"),
    api.getShiftStatus(),
    api.listOperators(),
  ]);

  const now = Date.now();
  if (chatsResponse.status === "fulfilled") {
    const chats = chatsResponse.value.items.map((chat) => computeUiChat(chat, now));
    set((state) => ({
      chats: sortChats(chats),
      selectedChatId: state.selectedChatId ?? chats[0]?.id ?? null,
    }));
  }
  if (statsResponse.status === "fulfilled") set({ slaStats: statsResponse.value });
  if (operatorsResponse.status === "fulfilled") set({ operators: operatorsResponse.value.items });

  const selected = get().selectedChatId;
  if (selected !== null) await get().loadMessages(selected);
}

function connectWs(get: GetFn, set: SetFn): void {
  const token = api.accessToken;
  if (!token) return;

  wsClient?.disconnect();
  wsClient = new WsClientImpl({
    url: import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws",
    token,
    onMessage: (envelope) => get().handleWsEnvelope(envelope),
    onStatus: (status, meta) => {
      const connection: ConnectionState = status === "open" ? "open" : status === "connecting" ? "connecting" : "closed";
      set({ connection, wsLatencyMs: meta.latencyMs });
    },
  });
  wsClient.connect();

  if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer);
  heartbeatTimer = window.setInterval(() => {
    if (api.accessToken) void api.heartbeat().catch(() => undefined);
  }, 20_000);
}

/* Auto-refresh of the whole chat list every 30s as a safety net (WS is primary). */
let pollTimer: number | null = null;
export function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = window.setInterval(async () => {
    const state = useCRMStore.getState();
    if (state.mode !== "live" || !state.operator) return;
    try {
      const response = await api.listChats(state.filter);
      const now = Date.now();
      useCRMStore.setState((current) => {
        const byId = new Map(current.chats.map((chat) => [chat.id, chat]));
        const merged = response.items.map((incoming) => {
          const previous = byId.get(incoming.id);
          return previous
            ? computeUiChat({ ...incoming, unread_count: previous.unread_count }, now)
            : computeUiChat(incoming, now);
        });
        return { chats: sortChats(merged) };
      });
    } catch {
      /* offline — WS reconnect logic handles it */
    }
  }, 30_000);
}

export function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

/* ------------------------------------------------------ stable selectors
 *
 * Zustand compares selector results by reference. A selector that derives a
 * *new* array/file resets identity — reading `messagesByChat[chatId] ?? []`
 * allocates a fresh `[]` whenever the map has no entry yet, which makes
 * `useSyncExternalStore` loop forever ("getSnapshot should be cached").
 * The module-level constants below make the empty results referentially stable,
 * and `selectVisibleChats` memoises against the source array identity.
 */

const EMPTY_MESSAGES: Message[] = [];
const EMPTY_CHATS: UiChatItem[] = [];
const EMPTY_SEARCH_HITS: SearchHit[] = [];

const visibleChatsCache = new WeakMap<UiChatItem[], { filter: ChatFilter; operatorId: number | null; result: UiChatItem[] }>();

/** Chats for the active filter — identity-stable until `chats`, filter or user change. */
export const selectVisibleChats = (state: CRMState): UiChatItem[] => {
  if (state.chats.length === 0) return EMPTY_CHATS;
  const operatorId = state.operator?.id ?? null;
  const cached = visibleChatsCache.get(state.chats);
  if (cached && cached.filter === state.filter && cached.operatorId === operatorId) return cached.result;

  const result = state.chats.filter((chat) => matchFilter(chat, state.filter, operatorId));
  visibleChatsCache.set(state.chats, { filter: state.filter, operatorId, result });
  return result;
};

export const selectSelectedChat = (state: CRMState): UiChatItem | null =>
  state.selectedChatId === null ? null : (state.chats.find((chat) => chat.id === state.selectedChatId) ?? null);

export const selectSelectedMessages = (state: CRMState): Message[] =>
  state.selectedChatId === null ? EMPTY_MESSAGES : (state.messagesByChat[state.selectedChatId] ?? EMPTY_MESSAGES);

/* -------------------------------- CONTRACTS_V3 — stable edit/forward selectors
 *
 * Same rule as above: `?? []` inside a selector allocates a new array on every
 * store read, which makes `useSyncExternalStore` spin. These helpers return the
 * shared `EMPTY_EDITS` / `null` before ever touching a map.
 */

/** Edit history of one message — identity-stable until that history changes. */
export const selectEditHistory = (messageId: number): ((state: CRMState) => MessageEdit[]) => {
  const selector = (state: CRMState): MessageEdit[] => {
    const items = state.editHistory[messageId];
    return items === undefined ? EMPTY_EDITS : items;
  };
  return selector;
};

/** `true` when the history block of that message is expanded. */
export const selectEditHistoryOpen = (messageId: number): ((state: CRMState) => boolean) => {
  const selector = (state: CRMState): boolean => state.editHistoryOpen[messageId] === true;
  return selector;
};

/** Load flag of one message's history. */
export const selectEditHistoryLoading = (messageId: number): ((state: CRMState) => boolean) => {
  const selector = (state: CRMState): boolean => state.editHistoryLoading[messageId] === true;
  return selector;
};

/** Last load error of one message's history (`null` when fine). */
export const selectEditHistoryError = (messageId: number): ((state: CRMState) => string | null) => {
  const selector = (state: CRMState): string | null => state.editHistoryError[messageId] ?? null;
  return selector;
};

export const getWsClient = (): WsClient | null => wsClient;

/* --------------------------- CONTRACTS_V5 §1/§5 — permission & quick replies
 *
 * Same referential-stability rule as above: a selector must never allocate a new
 * array on every read, otherwise `useSyncExternalStore` loops on a fresh snapshot.
 */

/**
 * Roles the backend treats as privileged for editing/deleting (§1.3 /
 * CONTRACTS_V3 §2.3): they pass the gate without `can_delete_messages`.
 */
const PRIVILEGED_ROLES: readonly Operator["role"][] = ["SUPERVISOR", "ADMIN"];

/**
 * Whether the signed-in operator may delete a message.
 *
 * The backend gate is `operator.can_delete_messages or operator.is_privileged`
 * (`services/message_delete._operator_outranks`). `OperatorOut` — what
 * `/api/auth/me` and `SHIFT_CHANGED` carry — exposes only `role`, so an explicit
 * `can_delete_messages` flag is honoured when a payload happens to include it and
 * the role is the fallback. The server remains authoritative: a stale `true` here
 * surfaces as the documented `403` toast.
 */
export const selectCanDeleteMessages = (state: CRMState): boolean => {
  const operator = state.operator;
  if (!operator) return false;
  const withFlag = operator as Operator & { can_delete_messages?: boolean };
  if (withFlag.can_delete_messages === true) return true;
  if (withFlag.can_delete_messages === false && !PRIVILEGED_ROLES.includes(operator.role)) return false;
  return PRIVILEGED_ROLES.includes(operator.role);
};

/**
 * Active replies only — what the composer renders as chips (§5.2).
 *
 * The derived array is memoised against the source-array identity (`quickReplies`
 * is only replaced when the list really changes, so `WeakMap` hits are the norm).
 * Returning a fresh `.filter()` result on every store read would change
 * `useSyncExternalStore`'s snapshot identity each time and throw
 * "The result of getSnapshot should be cached" — the same trap documented above
 * for `selectVisibleChats`.
 */
const activeQuickRepliesCache = new WeakMap<QuickReply[], QuickReply[]>();

export const selectActiveQuickReplies = (state: CRMState): QuickReply[] => {
  const source = state.quickReplies;
  if (source.length === 0) return EMPTY_QUICK_REPLIES;
  const cached = activeQuickRepliesCache.get(source);
  if (cached) return cached;
  const active = source.filter((item) => item.is_active);
  /* Every reply inactive is a real state; still return the shared empty array so
     the identity stays stable across renders. */
  const result = active.length === 0 ? EMPTY_QUICK_REPLIES : active;
  activeQuickRepliesCache.set(source, result);
  return result;
};

/** `true` while any quick-reply mutation is on the wire (disables editor controls). */
export const selectQuickReplyMutating = (state: CRMState): boolean => state.quickReplyMutating;


/* --------------------------------------- CONTRACTS_V5 §6 — search selector
 *
 * Same referential-stability rule as the selectors above: the overlay reads
 * `{items, hitCount}` from here on every keystroke, so a fresh array/object per
 * call would spin `useSyncExternalStore`. The cache is keyed by the results
 * array identity, which only changes when a search actually committed.
 */

interface SearchView {
  items: SearchHit[];
  /** `true` when a non-empty query produced zero hits. */
  isEmpty: boolean;
}

const searchViewCache = new WeakMap<SearchHit[], SearchView>();

export const selectSearchResults = (state: CRMState): SearchHit[] =>
  state.searchResults.length === 0 ? EMPTY_SEARCH_HITS : state.searchResults;

/** Search hits + a derived "nothing found" flag, identity-stable. */
export const selectSearchView = (state: CRMState): SearchView => {
  const items = selectSearchResults(state);
  if (items === EMPTY_SEARCH_HITS) return EMPTY_SEARCH_VIEW;
  const cached = searchViewCache.get(items);
  if (cached) return cached;
  const view: SearchView = { items, isEmpty: items.length === 0 };
  searchViewCache.set(items, view);
  return view;
};

const EMPTY_SEARCH_VIEW: SearchView = { items: EMPTY_SEARCH_HITS, isEmpty: true };
