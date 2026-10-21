/**
 * Typed REST client for the TG-Desk CRM backend (CONTRACTS.md §3).
 *
 * - One `ApiClient` instance per session, holding the bearer token.
 * - Every call raises `ApiError` with the backend's `detail` so the store can
 *   surface precise messages ("Chat is locked by another operator", ...).
 * - `fetch` is a plain global call so a mock transport can be layered on top.
 */

import type {
  Attachment,
  ChatFilter,
  ChatListItem,
  DeletedMessageOut,
  LoginResponse,
  Message,
  MessageEdit,
  MessageListResponse,
  Operator,
  PersonalSlaStats,
  QuickReply,
  QuickReplyCreatePayload,
  QuickReplyListResponse,
  QuickReplyUpdatePayload,
  ReactionSummary,
  SearchResponse,
  SearchSenderFilter,
  SendMessageRequest,
  ShiftStatus,
  SystemInfo,
  TxAdminPanel,
  TxLookupResponse,
} from "./types";

/** Response of `PATCH /api/messages/{id}` (CONTRACTS_V3 §2.3). */
export interface EditMessageResponse {
  /** The mutated message, exactly as stored. */
  message: Message;
  /** `true` when the edit was pushed to Telegram via `tg_outgoing.edit_tg_msg_id`. */
  edited_in_telegram: boolean;
}

/** Response of `POST /api/admin/login` (CONTRACTS_V2 §3.3). */
export interface AdminLoginResponse {
  access_token: string;
  token_type: string;
  expires_at?: string | null;
  operator: {
    id: number;
    username: string;
    display_name: string;
    role: string;
    shift_status: string;
    is_active: boolean;
    max_chats: number | null;
    can_delete_messages: boolean;
    can_export: boolean;
    avatar_color: string | null;
  };
}

/**
 * `AdminSettingsOut` — global SLA / application settings (CONTRACTS_V5 §2.2).
 *
 * Single-row `app_settings` table behind `GET|PATCH /api/admin/settings`:
 * `sla_target_seconds` is clamped to 60..86400 by the backend `CHECK`,
 * `updated_by` is `null` when nobody has saved the record yet.
 */
export interface AdminSettings {
  sla_enabled: boolean;
  sla_target_seconds: number;
  sla_reset_on_operator_reply: boolean;
  updated_at: string;
  updated_by: string | null;
}

export class ApiError extends Error {
  public readonly status: number;
  public readonly payload: unknown;

  constructor(status: number, message: string, payload?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }

  /** True for 401/403 — the store reacts by dropping the session. */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** True for 409 — the caller should react to the collision lock. */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

const DEFAULT_API_URL = "http://localhost:8000";

function resolveApiBase(): string {
  const configured = import.meta.env.VITE_API_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");
  return DEFAULT_API_URL;
}

function resolveWsBase(): string {
  const configured = import.meta.env.VITE_WS_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");
  const http = resolveApiBase().replace(/^http/, "ws");
  return `${http}/ws`;
}

export const API_BASE_URL = resolveApiBase();
export const WS_BASE_URL = resolveWsBase();

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Skip the Authorization header (login only). */
  anonymous?: boolean;
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 20_000;

export class ApiClient {
  private token: string | null = null;

  constructor(private readonly baseUrl: string = API_BASE_URL) {}

  get accessToken(): string | null {
    return this.token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  /** Low-level request helper with timeout, JSON handling and typed errors. */
  private async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { method = "GET", body, signal, anonymous = false, timeoutMs = DEFAULT_TIMEOUT_MS } = options;

    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    if (signal) {
      signal.addEventListener("abort", () => controller.abort(), { once: true });
    }

    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (!anonymous && this.token) headers.Authorization = `Bearer ${this.token}`;

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        mode: "cors",
        credentials: "omit",
      });
    } catch (error) {
      window.clearTimeout(timer);
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ApiError(408, "Request timed out — the backend did not answer.");
      }
      throw new ApiError(0, "Cannot reach the backend. Is uvicorn running on :8000?");
    } finally {
      window.clearTimeout(timer);
    }

    const rawText = await response.text();
    const data: unknown = rawText ? safeJsonParse(rawText) : null;

    if (!response.ok) {
      const detail = extractDetail(data) ?? `HTTP ${response.status}`;
      throw new ApiError(response.status, detail, data);
    }

    return data as T;
  }

  /* ------------------------------------------------------------- auth */
  login(username: string, password: string): Promise<LoginResponse> {
    return this.request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: { username, password },
      anonymous: true,
    });
  }

  me(): Promise<Operator> {
    return this.request<Operator>("/api/auth/me");
  }

  logout(): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  }

  /* ------------------------------------------------------------ shift */
  getShiftStatus(): Promise<PersonalSlaStats> {
    return this.request<PersonalSlaStats>("/api/shift/status");
  }

  setShiftStatus(status: ShiftStatus): Promise<Operator> {
    return this.request<Operator>("/api/shift/status", { method: "POST", body: { status } });
  }

  listOperators(): Promise<{ items: Operator[] }> {
    return this.request<{ items: Operator[] }>("/api/shift/operators");
  }

  heartbeat(): Promise<{ ok: boolean; server_time: string }> {
    return this.request<{ ok: boolean; server_time: string }>("/api/shift/heartbeat", { method: "POST" });
  }

  slaStats(): Promise<PersonalSlaStats> {
    return this.request<PersonalSlaStats>("/api/sla/stats/me");
  }

  /* ------------------------------------------------------------ chats */
  listChats(filter: ChatFilter = "all"): Promise<{ items: ChatListItem[] }> {
    return this.request<{ items: ChatListItem[] }>(`/api/chats?filter=${encodeURIComponent(filter)}`);
  }

  listMessages(chatId: number, limit = 100, beforeId?: number): Promise<MessageListResponse> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (beforeId !== undefined) query.set("before_id", String(beforeId));
    return this.request<MessageListResponse>(`/api/chats/${chatId}/messages?${query.toString()}`);
  }

  markRead(chatId: number): Promise<{ ok: boolean; unread_count: number }> {
    return this.request<{ ok: boolean; unread_count: number }>(`/api/chats/${chatId}/read`, {
      method: "POST",
    });
  }

  assignChat(chatId: number, operatorId: number | null): Promise<ChatListItem> {
    return this.request<ChatListItem>(`/api/chats/${chatId}/assign`, {
      method: "POST",
      body: { operator_id: operatorId },
    });
  }

  sendTyping(chatId: number, action: "start" | "stop"): Promise<{ ok: boolean; locked_by: string | null }> {
    return this.request<{ ok: boolean; locked_by: string | null }>(`/api/chats/${chatId}/typing`, {
      method: "POST",
      body: { action },
      timeoutMs: 8000,
    });
  }

  /* --------------------------------------------------------- messages */
  sendMessage(chatId: number, payload: SendMessageRequest): Promise<Message> {
    return this.request<Message>(`/api/chats/${chatId}/messages`, {
      method: "POST",
      body: payload,
      timeoutMs: 15_000,
    });
  }

  /* ------------------------------------------------------ tx lookups */
  lookupTx(txId: string, panel?: TxAdminPanel | null): Promise<TxLookupResponse> {
    return this.request<TxLookupResponse>("/api/tx/lookup", {
      method: "POST",
      body: panel ? { tx_id: txId, panel } : { tx_id: txId },
      timeoutMs: 15_000,
    });
  }

  /* ----------------------------------------------------------- system */
  health(): Promise<{ status: string; db: boolean; redis: boolean }> {
    return this.request<{ status: string; db: boolean; redis: boolean }>("/health", { anonymous: true });
  }

  systemInfo(): Promise<SystemInfo> {
    return this.request<SystemInfo>("/api/system/info");
  }

  /* ------------------------------------------------- CONTRACTS_V2 — media */

  getAttachments(messageId: number): Promise<{ items: Attachment[] }> {
    return this.request<{ items: Attachment[] }>(`/api/messages/${messageId}/attachments`);
  }

  /**
   * Uploads files to `chatId` as multipart/form-data.
   *
   * The Content-Type header is deliberately left unset so the browser appends its own
   * multipart boundary; setting it manually corrupts the request body.
   */
  async uploadAttachment(
    chatId: number,
    files: File[],
    text?: string,
    replyToId?: number | null,
  ): Promise<Message[]> {
    if (files.length === 0) throw new ApiError(400, "Не выбран ни один файл");

    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    if (text && text.trim().length > 0) form.append("text", text);
    if (replyToId != null) form.append("reply_to_id", String(replyToId));

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/api/chats/${chatId}/attachments`, {
        method: "POST",
        headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
        body: form,
        mode: "cors",
        credentials: "omit",
      });
    } catch {
      throw new ApiError(0, "Нет связи с сервером. Запущен ли uvicorn на :8000?");
    }

    const rawText = await response.text();
    const data: unknown = rawText ? safeJsonParse(rawText) : null;
    if (!response.ok) {
      throw new ApiError(response.status, extractDetail(data) ?? `HTTP ${response.status}`, data);
    }

    // The endpoint returns a single MessageOut today; accept an `{items: []}` shape too
    // so a future multi-file response needs no client change.
    if (data && typeof data === "object" && Array.isArray((data as { items?: unknown }).items)) {
      return (data as { items: Message[] }).items;
    }
    return [data as Message];
  }

  /**
   * Builds an absolute media URL carrying the bearer token as a query parameter.
   *
   * `<img src>` cannot attach an Authorization header, so the backend accepts `?token=`
   * as a fallback for media reads (CONTRACTS_V2 §3.1). Handles relative paths, absolute
   * URLs, and URLs that already carry a query string (`?variant=thumb` -> `&token=`).
   */
  mediaUrl(url: string): string {
    if (!url) return url;
    const absolute = /^https?:\/\//i.test(url)
      ? url
      : `${this.baseUrl}${url.startsWith("/") ? url : `/${url}`}`;
    if (!this.token) return absolute;
    const separator = absolute.includes("?") ? "&" : "?";
    return `${absolute}${separator}token=${encodeURIComponent(this.token)}`;
  }

  /* --------------------------------------------- CONTRACTS_V2 — reactions */

  getReactions(messageId: number): Promise<{ items: ReactionSummary[] }> {
    return this.request<{ items: ReactionSummary[] }>(`/api/messages/${messageId}/reactions`);
  }

  putReaction(messageId: number, emoji: string): Promise<{ items: ReactionSummary[] }> {
    return this.request<{ items: ReactionSummary[] }>(`/api/messages/${messageId}/reaction`, {
      method: "PUT",
      body: { emoji },
    });
  }

  deleteReaction(messageId: number): Promise<{ items: ReactionSummary[] }> {
    return this.request<{ items: ReactionSummary[] }>(`/api/messages/${messageId}/reaction`, {
      method: "DELETE",
    });
  }

  /* ------------------------------------- CONTRACTS_V3 — edits / forwarding */

  /**
   * `PATCH /api/messages/{id}` — edit an existing message (CONTRACTS_V3 §2.3).
   *
   * The backend also queues the Telegram-side `edit_message` call, so the
   * response reports whether it succeeded (`edited_in_telegram`) while the CRM
   * text is already stored. `409` is returned for a message deleted in
   * Telegram (invariant §5.5), `403` when the operator may not edit it.
   */
  editMessage(messageId: number, text: string): Promise<EditMessageResponse> {
    return this.request<EditMessageResponse>(`/api/messages/${messageId}`, {
      method: "PATCH",
      body: { text },
      timeoutMs: 15_000,
    });
  }

  /** `GET /api/messages/{id}/edits` — append-only Telegram edit history (§2.3). */
  getMessageEdits(messageId: number): Promise<{ items: MessageEdit[] }> {
    return this.request<{ items: MessageEdit[] }>(`/api/messages/${messageId}/edits`);
  }

  /**
   * `POST /api/messages/{id}/forward` — copy a message into another chat (§1.2).
   *
   * The backend never calls Telegram's `forward_messages`: the text and media are
   * re-sent as our own message, so `forwarded_from` stays `NULL` in the target
   * chat (invariant §5.1). `comment`, when given, is prepended above the copy.
   */
  forwardMessage(messageId: number, targetChatId: number, comment?: string): Promise<Message> {
    const trimmed = comment?.trim() ?? "";
    return this.request<Message>(`/api/messages/${messageId}/forward`, {
      method: "POST",
      body: trimmed ? { target_chat_id: targetChatId, comment: trimmed } : { target_chat_id: targetChatId },
      timeoutMs: 30_000,
    });
  }

  /* ------------------------------------------- CONTRACTS_V5 §6 — search */

  /**
   * `GET /api/search` — case-insensitive *substring* search across every chat
   * (CONTRACTS_V5 §6.2, invariant §8.8: `pg_trgm`, not `tsvector`).
   *
   * `q` is always percent-encoded (it may contain `#`, `&`, `+`, spaces or
   * Cyrillic), and non-actionable parameters are omitted so the URL stays a
   * clean cache key. `signal` lets the caller abandon a superseded keystroke.
   */
  searchMessages(params: {
    q: string;
    chat_id?: number;
    sender?: SearchSenderFilter;
    limit?: number;
    offset?: number;
    signal?: AbortSignal;
  }): Promise<SearchResponse> {
    const query = new URLSearchParams({ q: params.q });
    if (params.chat_id !== undefined) query.set("chat_id", String(params.chat_id));
    if (params.sender !== undefined && params.sender !== "all") query.set("sender", params.sender);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    return this.request<SearchResponse>(`/api/search?${query.toString()}`, { signal: params.signal });
  }

  /* ------------------------------------------------- CONTRACTS_V2 — admin */

  adminLogin(username: string, password: string): Promise<AdminLoginResponse> {
    return this.request<AdminLoginResponse>("/api/admin/login", {
      method: "POST",
      body: { username, password },
      anonymous: true,
    });
  }

  /* ---------------------------------- CONTRACTS_V5 §2 — SLA app settings */

  /**
   * `GET /api/admin/settings` — global application / SLA settings
   * (CONTRACTS_V5 §2.2). Administrator token only.
   *
   * The backend answers a flat `AdminSettingsOut` object, never a wrapper, so it
   * is returned as-is and the caller validates the fields it depends on.
   */
  adminGetSettings(): Promise<AdminSettings> {
    return this.request<AdminSettings>("/api/admin/settings");
  }

  /**
   * `PATCH /api/admin/settings` — partial update of the same record (§2.2).
   *
   * Only the keys present in `payload` are sent, so an untouched field is never
   * overwritten. The response is the full, freshly stored `AdminSettingsOut`.
   */
  adminUpdateSettings(payload: Partial<AdminSettings>): Promise<AdminSettings> {
    return this.request<AdminSettings>("/api/admin/settings", {
      method: "PATCH",
      body: payload,
      timeoutMs: 15_000,
    });
  }

  /* ---------------------------------------- CONTRACTS_V5 §1 — deletion */

  /**
   * `DELETE /api/messages/{id}` — soft-delete a message from the CRM (§1.3).
   *
   * The row is **never** removed from `group_messages` (invariant §8.1); it is
   * flagged `is_deleted_in_tg = true` with `deleted_at` / `deleted_source = 'CRM'`
   * and the response carries the full updated `MessageOut`, so the caller can
   * repaint the bubble without re-fetching the timeline.
   *
   * Error contract (§1.3): `404` unknown message, `409` already deleted,
   * `403` operator lacks `can_delete_messages`. A Telegram mirror failure is
   * reported in `telegram.delivered` / `telegram.error` and is **not** an HTTP
   * error — the CRM state is committed regardless (invariant §8.3).
   *
   * The timeout is generous: the handler publishes `tg_delete_out` and waits for
   * the worker ACK path before answering.
   */
  deleteMessage(messageId: number): Promise<DeletedMessageOut> {
    return this.request<DeletedMessageOut>(`/api/messages/${messageId}`, {
      method: "DELETE",
      timeoutMs: 30_000,
    });
  }

  /* ------------------------------------ CONTRACTS_V5 §5 — quick replies */

  /**
   * `GET /api/quick-replies` — canned replies, sorted server-side (§5.2).
   *
   * Only active replies are returned unless `all` is set, which additionally
   * lists the inactive ones and is therefore administrator-only on the backend.
   */
  listQuickReplies(all = false): Promise<QuickReplyListResponse> {
    const query = all ? "?all=true" : "";
    return this.request<QuickReplyListResponse>(`/api/quick-replies${query}`);
  }

  /** `POST /api/quick-replies` — create a reply; `201 QuickReplyOut` (§5.2). */
  createQuickReply(payload: QuickReplyCreatePayload): Promise<QuickReply> {
    return this.request<QuickReply>("/api/quick-replies", {
      method: "POST",
      body: payload,
      timeoutMs: 15_000,
    });
  }

  /**
   * `PATCH /api/quick-replies/{id}` — partial update (§5.2).
   *
   * The backend refuses an empty patch with `422`, so callers must send at least
   * one field; `409` means the new `shortcut` collides with another row.
   */
  updateQuickReply(id: number, payload: QuickReplyUpdatePayload): Promise<QuickReply> {
    return this.request<QuickReply>(`/api/quick-replies/${id}`, {
      method: "PATCH",
      body: payload,
      timeoutMs: 15_000,
    });
  }

  /**
   * `DELETE /api/quick-replies/{id}` — hard delete, answering `204 No Content`.
   *
   * `request<T>` already tolerates an empty body (it parses `""` as `null`), so
   * the void result is correct rather than a parsing hazard.
   */
  deleteQuickReply(id: number): Promise<void> {
    return this.request<void>(`/api/quick-replies/${id}`, {
      method: "DELETE",
      timeoutMs: 15_000,
    });
  }
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function extractDetail(data: unknown): string | null {
  if (typeof data === "string") return data;
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string } | undefined;
      return first?.msg ?? "Validation error";
    }
  }
  return null;
}

/** Shared singleton used by the Zustand store. */
export const api = new ApiClient();
