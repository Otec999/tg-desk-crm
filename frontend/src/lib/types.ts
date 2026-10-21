/**
 * TypeScript mirror of `CONTRACTS.md` §2/§3/§4.
 *
 * Every field name is snake_case on purpose: these objects travel verbatim over
 * the wire (REST + WebSocket) and any renaming here would silently break the
 * connection between FastAPI and the UI.
 */

/* ------------------------------------------------------------------ enums */

export type ShiftStatus = "ONLINE" | "PAUSE" | "OFFLINE";
/** CONTRACTS_V2 §1 — `ADMIN` unlocks the separate admin console (§3.3). */
export type OperatorRole = "OPERATOR" | "SUPERVISOR" | "ADMIN";
export type DeliveryStatus = "pending" | "sent" | "failed";
export type SlaIncidentStatus = "PENDING" | "RESOLVED";
export type TxAdminPanel = "ADMIN_ONE" | "ADMIN_TWO";
export type MessageSenderType = "OPERATOR" | "CLIENT" | "BOT" | "SYSTEM";
export type ChatFilter = "all" | "mine" | "burning";

export const SHIFT_STATUSES: readonly ShiftStatus[] = ["ONLINE", "PAUSE", "OFFLINE"];
export const CHAT_FILTERS: readonly ChatFilter[] = ["all", "mine", "burning"];

/* -------------------------------------------------------------- operators */

export interface Operator {
  id: number;
  username: string;
  display_name: string;
  role: OperatorRole;
  shift_status: ShiftStatus;
  is_online_ws: boolean;
  last_seen_at: string | null;
  sla_target_seconds: number;
  created_at: string;
}

export interface PersonalSlaStats {
  operator_id: number;
  shift_status: ShiftStatus;
  avg_response_seconds: number;
  resolved_count: number;
  open_count: number;
  overdue_count: number;
  sla_percent: number;
  sla_target_seconds: number;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  operator: Operator;
}

/* ------------------------------------------------------------------ chats */

export interface SlaSnapshot {
  status: SlaIncidentStatus;
  deadline_at: string;
  remaining_seconds: number;
  is_overdue: boolean;
}

export interface ChatLastMessage {
  id: number;
  text: string;
  sender_name: string;
  is_outgoing: boolean;
  created_at: string;
}

export interface ChatListItem {
  id: number;
  tg_chat_id: number | null;
  title: string;
  username: string | null;
  merchant_code: string | null;
  unread_count: number;
  assigned_operator_id: number | null;
  assigned_operator_name: string | null;
  last_message: ChatLastMessage | null;
  sla: SlaSnapshot | null;
}

/* --------------------------------------------------------------- messages */

/** Media flavour of a message (mirrors `MessageKind` in the backend, CONTRACTS_V2 §1). */
export const MESSAGE_KINDS = [
  "TEXT",
  "PHOTO",
  "DOCUMENT",
  "VOICE",
  "VIDEO",
  "STICKER",
  "AUDIO",
  "OTHER",
] as const;

export type MessageKind = (typeof MESSAGE_KINDS)[number];

export interface Message {
  id: number;
  chat_id: number;
  tg_msg_id: number | null;
  sender_type: MessageSenderType;
  sender_operator_id: number | null;
  sender_name: string;
  sender_tg_id: number | null;
  text: string;
  reply_to_id: number | null;
  reply_to_tg_msg_id: number | null;
  phone: string | null;
  is_deleted_in_tg: boolean;
  deleted_at: string | null;
  sla_incident_id: number | null;
  delivery_status: DeliveryStatus;
  is_outgoing: boolean;
  /** Idempotency key echoed back by the backend for optimistic sends. */
  client_nonce?: string | null;
  created_at: string;
  /* ------------------------------------------------ CONTRACTS_V2 §3.1/§3.2 */
  /** Media attached to this message (empty/absent for plain text). */
  attachments?: Attachment[];
  /** Aggregated emoji counters for this message. */
  reactions?: ReactionSummary[];
  /** Media flavour; absent on legacy rows means `TEXT`. */
  kind?: MessageKind | string;
  /** Media caption (also mirrored into `text` by the backend). */
  caption?: string | null;
  /** Original author when the message was forwarded ("forwarded from"). */
  forwarded_from?: string | null;
  /** `@username` of the sender inside Telegram, when known. */
  sender_username?: string | null;
  /* ------------------------------------------------ CONTRACTS_V3 §2.2 */
  /**
   * Moment the message text was last changed (either from CRM or from
   * Telegram). Absent/`null` means "never edited".
   */
  edited_at?: string | null;
  /* ------------------------------------------------ CONTRACTS_V5 §1.2 */
  /**
   * Who triggered the deletion, or `null`/absent while the message is alive.
   *
   * The backend keeps `is_deleted_in_tg` and `deleted_source` in lockstep through
   * a `CHECK` constraint (§1.2), so a `true` flag always carries a source. The
   * field stays optional on the wire so a row read before the V5 migration (or a
   * bundled mock fixture) still type-checks; the UI then falls back to neutral
   * wording instead of guessing the origin.
   */
  deleted_source?: DeletedSource | null;
}

/* ----------------------------------------------- CONTRACTS_V3 §2.2 — edits */

/** Who produced an edit row. Only `TELEGRAM` rows are ever persisted. */
export type MessageEditSource = "TELEGRAM" | "CRM";

/**
 * One entry of the append-only edit history (mirrors `MessageEditOut`,
 * CONTRACTS_V3 §2.2/§2.3). CRM edits produce no row, so `source` is
 * `TELEGRAM` in practice — the union keeps the wire honest if that changes.
 */
export interface MessageEdit {
  id: number;
  message_id: number;
  source: MessageEditSource;
  old_text: string;
  new_text: string;
  operator_id: number | null;
  actor_name: string | null;
  created_at: string;
}

export interface MessageListResponse {
  items: Message[];
  has_more: boolean;
}

export interface SendMessageRequest {
  text: string;
  reply_to_msg_id?: number | null;
  client_nonce?: string | null;
}

/* ------------------------------------------------------- transaction lookup */

export type TxStatusValue =
  | "COMPLETED"
  | "PROCESSING"
  | "PENDING"
  | "FAILED"
  | "DECLINED"
  | "REFUNDED"
  | "NOT_FOUND"
  | "ERROR";

export interface TxPanelResult {
  panel: TxAdminPanel;
  found: boolean;
  tx_id: string;
  status: TxStatusValue | string;
  amount: string | null;
  currency: string | null;
  provider: string | null;
  error_reason: string | null;
  external_ref: string | null;
  raw: Record<string, unknown>;
}

export interface TxLookupResponse {
  tx_id: string;
  looked_up_at: string;
  admin_one: TxPanelResult;
  admin_two: TxPanelResult;
}

export interface TxLookupRequest {
  tx_id: string;
  panel?: TxAdminPanel | null;
}

/* ------------------------------------------------------------ typing lock */

export interface TypingLockState {
  chat_id: number;
  operator_id: number;
  operator_name: string;
  until: string;
}

/* ------------------------------------------------------------ websocket WS */

export interface WsEnvelope<T = unknown> {
  type: string;
  ts: string;
  data: T;
}

export interface WsNewMessageData extends Message {
  chat?: ChatListItem;
}

export interface WsMessageDeletedData {
  message_id: number;
  chat_id: number;
  deleted_at: string;
  message?: Message;
}

export interface WsSlaUpdateData {
  chat_id: number;
  incident_id: number;
  status: SlaIncidentStatus;
  deadline_at: string;
  remaining_seconds: number;
  assigned_operator_id: number | null;
  assigned_operator_name: string | null;
  is_overdue: boolean;
}

export interface WsSlaResolvedData {
  chat_id: number;
  incident_id: number;
  operator_id: number;
  response_time_seconds: number;
  stats?: PersonalSlaStats;
}

export interface WsTypingLockData {
  chat_id: number;
  operator_id: number;
  operator_name: string;
  until: string;
}

export interface WsTypingUnlockData {
  chat_id: number;
  /** ``null`` signals a forced release (socket closed / send completed) for any holder. */
  operator_id: number | null;
}

/* ------------------------------------------------------- CONTRACTS_V2 §3.1 */

/** One media attachment of a message. Mirrors `AttachmentOut` in the backend. */
export interface Attachment {
  id: number;
  message_id: number;
  kind: "PHOTO" | "DOCUMENT" | "VIDEO" | "VOICE" | "STICKER" | "AUDIO" | "OTHER" | string;
  file_name: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  /** Relative API path, e.g. `/api/media/1` — resolve through `api.mediaUrl()`. */
  url: string;
  /** Relative API path with `?variant=thumb`, or null when no preview exists. */
  thumbnail_url: string | null;
}

/* ------------------------------------------------------- CONTRACTS_V2 §3.2 */

/** Aggregated reactions for a single emoji on a message. */
export interface ReactionSummary {
  emoji: string;
  count: number;
  /** True when the calling operator is among the reactors. */
  me: boolean;
  operators: string[];
  operator_ids: number[];
}

export interface WsMessageReactionData {
  message_id: number;
  chat_id: number;
  emoji: string;
  count?: number;
  operator_id?: number | null;
  tg_user_id?: number | null;
}

export interface WsMessageReactionRemovedData {
  message_id: number;
  chat_id: number;
  emoji: string;
  operator_id?: number | null;
  tg_user_id?: number | null;
}

export interface WsMessageMediaData {
  message_id: number;
  chat_id: number;
  attachments: Attachment[];
}

export interface WsShiftChangedData extends Operator {}

export interface WsOperatorPresenceData {
  operator_id: number;
  is_online_ws: boolean;
  shift_status: ShiftStatus;
}

export interface WsChatAssignedData {
  chat_id: number;
  assigned_operator_id: number | null;
  assigned_operator_name: string | null;
}

export interface WsMessageStatusData {
  message_id: number;
  chat_id: number;
  delivery_status: DeliveryStatus;
  tg_msg_id: number | null;
}

export interface WsMessageEditedData {
  message_id: number;
  chat_id: number;
  text: string;
  edited_at: string;
  /* ------------------------------------------------ CONTRACTS_V3 §2.6 */
  /** `"TELEGRAM"` for a client-side edit, `"CRM"` for our own operator edit. */
  source?: MessageEditSource | string | null;
  /** New history row — present for `TELEGRAM` edits, `null` for CRM edits. */
  edit?: MessageEdit | null;
}

/* --------------------------------------------- CONTRACTS_V3 §2.6 — forward */

/**
 * `MESSAGE_FORWARDED` — the copy that landed in the *target* chat.
 *
 * `message` is the freshly created `MessageOut` of the target chat and `chat`
 * carries the refreshed list item, so the UI can update both timelines.
 */
export interface WsMessageForwardedData {
  source_message_id: number;
  source_chat_id: number;
  message: Message;
  chat?: ChatListItem | null;
}

/* ------------------------------------------- CONTRACTS_V5 §6 — global search */

/** Sender filter of `GET /api/search` (§6.2): `all` | `client` | `operator`. */
export type SearchSenderFilter = "all" | "client" | "operator";

export const SEARCH_SENDER_FILTERS: readonly SearchSenderFilter[] = ["all", "client", "operator"];

/**
 * One hit of the global search (mirrors `SearchHitOut`, CONTRACTS_V5 §6.2).
 *
 * `snippet` is the server-truncated excerpt and `match_spans` are its
 * `[start, end)` character offsets (JavaScript `slice`-compatible) used for the
 * `<mark>` highlighting; `score` is the backend's `similarity()` rank.
 */
export interface SearchHit {
  chat_id: number;
  chat_title: string;
  chat_merchant_code: string | null;
  message: Message;
  snippet: string;
  match_spans: [number, number][];
  score: number;
}

/** `GET /api/search` response (mirrors `SearchResponseOut`, CONTRACTS_V5 §6.2). */
export interface SearchResponse {
  items: SearchHit[];
  total: number;
  has_more: boolean;
  took_ms: number;
}

/* ------------------------------------------- CONTRACTS_V5 §1 — deletion */

/**
 * Origin of a soft-deleted message (§1.2).
 *
 * The backend stores it in `group_messages.deleted_source` and a `CHECK`
 * constraint guarantees it is non-`NULL` exactly when `is_deleted_in_tg` is
 * `true`, so the two can never disagree.
 */
export type DeletedSource = "CRM" | "TELEGRAM";

export const DELETED_SOURCES: readonly DeletedSource[] = ["CRM", "TELEGRAM"];

/**
 * Result of the Telegram mirror of a deletion (§1.3).
 *
 * `requested` — a `tg_delete_out` command was published (the message had both
 * `tg_msg_id` and its chat a `tg_chat_id`); `delivered` — that command reached
 * Redis. A `false` here does **not** mean the request failed: the CRM row is
 * already committed (§8 invariant 3), only the Telegram copy may still exist.
 */
export interface TelegramDeliveryInfo {
  requested: boolean;
  delivered: boolean;
  error: string | null;
}

/**
 * `DELETE /api/messages/{message_id}` response (mirrors `DeletedMessageOut`, §1.3).
 *
 * `message` is the full `MessageOut` of the now soft-deleted row — already
 * carrying `is_deleted_in_tg = true`, `deleted_at` and `deleted_source = "CRM"` —
 * so the store can repaint the bubble as a grey trace without a re-fetch.
 */
export interface DeletedMessageOut {
  message_id: number;
  chat_id: number;
  deleted_at: string;
  deleted_source: DeletedSource;
  message: Message;
  telegram: TelegramDeliveryInfo;
}

/* --------------------------------------- CONTRACTS_V5 §5 — quick replies */

/** Category applied when a reply does not pick one (§5.1 `DEFAULT 'general'`). */
export const QUICK_REPLY_DEFAULT_CATEGORY = "general";
/** `quick_replies.shortcut` cap (§5.1 `VARCHAR(32)`). */
export const QUICK_REPLY_SHORTCUT_MAX_LENGTH = 32;
/** `quick_replies.title` cap (§5.1 `VARCHAR(120)`). */
export const QUICK_REPLY_TITLE_MAX_LENGTH = 120;
/** `quick_replies.category` cap (§5.1 `VARCHAR(40)`). */
export const QUICK_REPLY_CATEGORY_MAX_LENGTH = 40;
/** Reply body cap — the backend mirrors `MAX_MESSAGE_TEXT_LENGTH` (§5.2). */
export const QUICK_REPLY_BODY_MAX_LENGTH = 4096;
/** Allowed `shortcut` characters: letters, digits, `-` and `_` (§5.2). */
export const QUICK_REPLY_SHORTCUT_PATTERN = /^[A-Za-z0-9_-]+$/;

/**
 * One editable canned reply (mirrors `QuickReplyOut`, CONTRACTS_V5 §5.2).
 *
 * `created_by` is the authoring operator's **display name** (not an id) and is
 * `null` for seeded rows or when the author's account was deleted, so the editor
 * shows it as a best-effort caption rather than resolving it.
 */
export interface QuickReply {
  id: number;
  /** Short code, unique and case-insensitive; typed after `/` in the composer. */
  shortcut: string;
  /** Caption shown in the quick-reply list and on the composer chip. */
  title: string;
  /** Text inserted into the composer. Never blank (§5.1 `CHECK`). */
  body: string;
  category: string;
  sort_order: number;
  is_active: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

/** `POST /api/quick-replies` body (§5.2). */
export interface QuickReplyCreatePayload {
  shortcut: string;
  title: string;
  body: string;
  category?: string;
  sort_order?: number;
  is_active?: boolean;
}

/**
 * `PATCH /api/quick-replies/{id}` body (§5.2).
 *
 * Every field is optional and the backend rejects an empty patch with `422`, so
 * callers must send at least one key.
 */
export interface QuickReplyUpdatePayload {
  shortcut?: string;
  title?: string;
  body?: string;
  category?: string;
  sort_order?: number;
  is_active?: boolean;
}

/** `GET /api/quick-replies` response — also the §5.3 WS payload. */
export interface QuickReplyListResponse {
  items: QuickReply[];
}

/** `QUICK_REPLIES_UPDATED` — fanned out to every operator after any change (§5.3). */
export interface WsQuickRepliesUpdatedData {
  items: QuickReply[];
}

/* ------------------------------------------------------------------ misc */

export interface HealthResponse {
  status: "ok" | "degraded" | string;
  db: boolean;
  redis: boolean;
  app_env?: string;
  version?: string;
}

export interface SystemInfo {
  telegram_connected: boolean;
  ws_clients: number;
  workers: Record<string, string>;
  sla_ticking: boolean;
}

export type ConnectionState = "connecting" | "open" | "closed" | "mock";

/** UI-only chat item enriched with client-side estimates for smooth rendering. */
export interface UiChatItem extends ChatListItem {
  /** Locally interpolated remaining seconds for the SLA countdown. */
  sla_remaining_seconds: number | null;
  /** True when a local timer produced the value (SLA_UPDATE refreshes it). */
  sla_local: boolean;
}
