/**
 * Admin panel store (Zustand) — CONTRACTS_V2.md §3.3 / §6, файл A9.
 *
 * Особенности, продиктованные прошлыми инцидентами этого проекта:
 *
 * - **Никаких новых массивов/объектов в селекторах.** Каждое поле стейта — это
 *   стабильная ссылка, меняющаяся только при реальном обновлении данных, поэтому
 *   `useAdminStore((s) => s.operators)` больше не вызывает бесконечных ререндеров.
 * - **Один запрос одного типа за раз** (флаги `operatorsLoading`, `accountsLoading`,
 *   `auditLoading`, `loginLoading`, `submitting`). Параллельный вызов того же действия
 *   переиспользует уже летящий промис, а не плодит дубликаты запросов.
 * - **401/403** выводят из панели: токен стирается, `operator` становится `null`,
 *   страница сама уводит на `/admin`.
 *
 * Токен и профиль администратора живут в `localStorage` под ключами
 * `tgdesk.admin_token` / `tgdesk.admin_operator` (отдельно от деск-сессии
 * `tgdesk.token` / `tgdesk.operator`, чтобы две сессии не затирали друг друга).
 */

import { create } from "zustand";

import { API_BASE_URL } from "../lib/api";

/* ------------------------------------------------------------- contracts */

export type AdminRole = "ADMIN" | "SUPERVISOR" | "OPERATOR";

export const ADMIN_ROLES: readonly AdminRole[] = ["ADMIN", "SUPERVISOR", "OPERATOR"];

export interface AdminOperator {
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
}

export interface AdminAccount {
  id: number;
  label: string;
  tg_user_id: number | null;
  tg_username: string | null;
  phone: string | null;
  api_id: number | null;
  is_active: boolean;
  has_session: boolean;
}

export interface AuditEntry {
  id: number;
  actor_username: string | null;
  action: string;
  target_type: string | null;
  target_id: number | null;
  detail: string | null;
  created_at: string | null;
}

/**
 * Глобальные настройки SLA (CONTRACTS_V5 §2.2, `AdminSettingsOut`).
 *
 * Одна строка `app_settings`; `sla_target_seconds` на сервере ограничен
 * `CHECK (sla_target_seconds BETWEEN 60 AND 86400)`, `updated_by` равен `null`,
 * пока запись ни разу не сохраняли.
 */
export interface AdminSettings {
  sla_enabled: boolean;
  sla_target_seconds: number;
  sla_reset_on_operator_reply: boolean;
  updated_at: string;
  updated_by: string | null;
}

/** Поля, которые разрешено менять через `PATCH /api/admin/settings`. */
export type AdminSettingsPatch = Partial<
  Pick<AdminSettings, "sla_enabled" | "sla_target_seconds" | "sla_reset_on_operator_reply">
>;

/** Границы `sla_target_seconds` — те же, что в `CHECK` бэкенда. */
export const SLA_TARGET_SECONDS_MIN = 60;
export const SLA_TARGET_SECONDS_MAX = 86_400;

export interface AdminLoginResult {
  access_token: string;
  token_type: string;
  expires_at: string | null;
  operator: AdminOperator;
  scope: string;
}

/* ---------------------------------------------------------------- inputs */

export interface OperatorCreateInput {
  username: string;
  display_name: string;
  password: string;
  role: AdminRole;
  max_chats: number | null;
  can_delete_messages: boolean;
  can_export: boolean;
  is_active: boolean;
  avatar_color: string | null;
}

/** `username` намеренно отсутствует: сервер считает его неизменяемым. */
export interface OperatorUpdateInput {
  display_name: string;
  role: AdminRole;
  max_chats: number | null;
  can_delete_messages: boolean;
  can_export: boolean;
  is_active: boolean;
  avatar_color: string | null;
}

export interface AccountCreateInput {
  label: string;
  tg_username: string | null;
  phone: string | null;
  api_id: number | null;
  api_hash: string | null;
  session_string: string | null;
  is_active: boolean;
}

/**
 * Обновление аккаунта. `api_hash`/`session_string` передаются только когда
 * оператор действительно вводит новые секреты — сервер НЕ отдаёт их обратно,
 * поэтому «пустое поле» означает «не трогать», а не «стереть».
 */
export interface AccountUpdateInput {
  label: string;
  tg_username: string | null;
  phone: string | null;
  api_id: number | null;
  api_hash: string | null;
  session_string: string | null;
  is_active: boolean;
}

/* --------------------------------------------------------------- helpers */

const TOKEN_KEY = "tgdesk.admin_token";
const OPERATOR_KEY = "tgdesk.admin_operator";

/** Лимит журнала — совпадает с `MAX_AUDIT_LIMIT` бэкенда. */
const AUDIT_LIMIT = 200;

const REQUEST_TIMEOUT_MS = 20_000;

const INVALID_CREDENTIALS_TEXT = "Неверный логин или пароль";
const ADMIN_REQUIRED_TEXT = "Требуются права администратора";

/** Значения, которые сервер возвращает на «нет данных» вместо `null`. */
const UNKNOWN_ROLE = "OPERATOR";
const UNKNOWN_SHIFT = "OFFLINE";

/* ----------------------------------------------------------- low-level HTTP */

class AdminApiError extends Error {
  public readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
  }

  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

interface HttpOptions {
  method?: HttpMethod;
  body?: unknown;
  token?: string | null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

/** Достаёт человекочитаемый `detail` из ответа FastAPI (строка или список ошибок). */
function extractDetail(payload: unknown): string | null {
  if (typeof payload === "string" && payload.trim()) return payload.trim();
  if (!isPlainObject(payload)) return null;
  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const messages: string[] = [];
    for (const item of detail) {
      if (!isPlainObject(item)) continue;
      const rawMessage = item.msg;
      if (typeof rawMessage !== "string") continue;
      const field = Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "";
      messages.push(field ? `${field}: ${rawMessage}` : rawMessage);
    }
    if (messages.length > 0) return messages.join("; ");
  }
  return null;
}

/**
 * Перевод типовых машинных сообщений API на русский.
 * Неизвестный текст остаётся как есть — потерять точную причину хуже,
 * чем показать её на английском.
 */
const DETAIL_TRANSLATIONS: ReadonlyArray<readonly [RegExp, string]> = [
  [/^invalid username or password$/i, INVALID_CREDENTIALS_TEXT],
  [/^invalid credentials$/i, INVALID_CREDENTIALS_TEXT],
  [/^not authenticated$/i, "Сессия истекла — войдите заново"],
  [/^could not validate credentials$/i, "Сессия истекла — войдите заново"],
  [/^internal server error$/i, "Внутренняя ошибка сервера"],
  [/^validation error$/i, "Некорректные данные формы"],
  [/^request timed out/i, "Сервер не ответил — повторите попытку"],
  [/^key is required$/i, "Укажите метку аккаунта"],
];

function humanizeDetail(detail: string | null): string | null {
  if (!detail) return null;
  for (const [pattern, message] of DETAIL_TRANSLATIONS) {
    if (pattern.test(detail)) return message;
  }
  return detail;
}

function normalizeError(error: unknown): string {
  if (error instanceof AdminApiError) {
    return humanizeDetail(error.message) ?? "Неизвестная ошибка";
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "Сервер не ответил — повторите попытку";
  }
  if (error instanceof Error) {
    return humanizeDetail(error.message) ?? "Не удалось выполнить запрос";
  }
  return "Не удалось выполнить запрос";
}

/** Единая точка выхода в API админки: `fetch` + Bearer + разбор `detail`. */
async function http<T>(path: string, options: HttpOptions = {}): Promise<T> {
  const { method = "GET", body, token } = options;

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      mode: "cors",
      credentials: "omit",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new AdminApiError(408, "Сервер не ответил — повторите попытку");
    }
    throw new AdminApiError(0, "Не удалось связаться с сервером (API на порту 8000)");
  } finally {
    window.clearTimeout(timer);
  }

  const rawText = await response.text();
  const payload: unknown = rawText ? safeJsonParse(rawText) : null;

  if (!response.ok) {
    const detail = extractDetail(payload);
    const fallback =
      response.status === 401
        ? INVALID_CREDENTIALS_TEXT
        : response.status === 403
          ? ADMIN_REQUIRED_TEXT
          : `Ошибка сервера (${response.status})`;
    throw new AdminApiError(response.status, detail ?? fallback);
  }

  return payload as T;
}

/* -------------------------------------------------------------- normalize */

function asRecord(value: unknown): Record<string, unknown> {
  return isPlainObject(value) ? value : {};
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function normalizeOperator(raw: unknown): AdminOperator | null {
  const record = asRecord(raw);
  const id = asNumber(record.id);
  const username = asString(record.username);
  if (id === null || username === null) return null;

  const displayName = asString(record.display_name) ?? username;
  const role = (asString(record.role) ?? UNKNOWN_ROLE).toUpperCase();

  return {
    id,
    username,
    display_name: displayName,
    role,
    shift_status: (asString(record.shift_status) ?? UNKNOWN_SHIFT).toUpperCase(),
    is_active: asBoolean(record.is_active, true),
    max_chats: asNumber(record.max_chats),
    can_delete_messages: asBoolean(record.can_delete_messages),
    can_export: asBoolean(record.can_export),
    avatar_color: asString(record.avatar_color),
  };
}

function normalizeAccount(raw: unknown): AdminAccount | null {
  const record = asRecord(raw);
  const id = asNumber(record.id);
  const label = asString(record.label);
  if (id === null || label === null) return null;

  return {
    id,
    label,
    tg_user_id: asNumber(record.tg_user_id),
    tg_username: asString(record.tg_username),
    phone: asString(record.phone),
    api_id: asNumber(record.api_id),
    is_active: asBoolean(record.is_active, true),
    has_session: asBoolean(record.has_session),
  };
}

function normalizeAuditEntry(raw: unknown): AuditEntry | null {
  const record = asRecord(raw);
  const id = asNumber(record.id);
  const action = asString(record.action);
  if (id === null || action === null) return null;

  return {
    id,
    actor_username: asString(record.actor_username),
    action,
    target_type: asString(record.target_type),
    target_id: asNumber(record.target_id),
    detail: asString(record.detail),
    created_at: asString(record.created_at),
  };
}

function normalizeList<T>(payload: unknown, normalize: (raw: unknown) => T | null): T[] {
  const items = asRecord(payload).items;
  if (!Array.isArray(items)) return [];
  const result: T[] = [];
  for (const item of items) {
    const normalized = normalize(item);
    if (normalized) result.push(normalized);
  }
  return result;
}

/**
 * `AdminSettingsOut` → `AdminSettings`. Возвращает `null` только когда ответ
 * не объект: отдельные поля чинятся дефолтами, чтобы одна кривая цифра не
 * блокировала всю вкладку SLA.
 */
function normalizeSettings(raw: unknown): AdminSettings | null {
  if (!isPlainObject(raw)) return null;

  const seconds = asNumber(raw.sla_target_seconds);
  const target = seconds === null ? 1800 : Math.round(seconds);

  return {
    sla_enabled: asBoolean(raw.sla_enabled, true),
    sla_target_seconds: Math.min(
      SLA_TARGET_SECONDS_MAX,
      Math.max(SLA_TARGET_SECONDS_MIN, target),
    ),
    sla_reset_on_operator_reply: asBoolean(raw.sla_reset_on_operator_reply, true),
    updated_at: asString(raw.updated_at) ?? "",
    updated_by: asString(raw.updated_by),
  };
}

function normalizeLogin(payload: unknown): AdminLoginResult | null {
  const record = asRecord(payload);
  const token = asString(record.access_token);
  const operator = normalizeOperator(record.operator);
  if (!token || !operator) return null;
  return {
    access_token: token,
    token_type: asString(record.token_type) ?? "bearer",
    expires_at: asString(record.expires_at),
    operator,
    scope: asString(record.scope) ?? "admin",
  };
}

/* ------------------------------------------------------------- persistence */

function readStoredToken(): string | null {
  try {
    const value = window.localStorage.getItem(TOKEN_KEY);
    return value && value.trim() !== "" ? value : null;
  } catch {
    return null;
  }
}

function readStoredOperator(): AdminOperator | null {
  try {
    const raw = window.localStorage.getItem(OPERATOR_KEY);
    if (!raw) return null;
    return normalizeOperator(safeJsonParse(raw));
  } catch {
    return null;
  }
}

function persistSession(token: string, operator: AdminOperator): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(OPERATOR_KEY, JSON.stringify(operator));
  } catch {
    /* приватный режим/переполнение хранилища: сессия живёт только в памяти */
  }
}

function clearStoredSession(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(OPERATOR_KEY);
  } catch {
    /* игнорируем: очистить память всё равно успеем */
  }
}

/* ------------------------------------------------------------- validation */

/** §3.3 `OperatorCreateRequest`: 3..64 символа из `[A-Za-z0-9_.-]`. */
const USERNAME_PATTERN = /^[A-Za-z0-9_.-]{3,64}$/;
const MAX_CHATS_LIMIT = 500;

export interface FieldErrors {
  username?: string;
  display_name?: string;
  password?: string;
  role?: string;
  max_chats?: string;
  label?: string;
  tg_username?: string;
  phone?: string;
  api_id?: string;
  api_hash?: string;
  session_string?: string;
}

export function validateOperatorCreate(input: OperatorCreateInput): FieldErrors {
  const errors: FieldErrors = {};
  const username = input.username.trim();
  const displayName = input.display_name.trim();

  if (username === "") {
    errors.username = "Укажите логин";
  } else if (!USERNAME_PATTERN.test(username)) {
    errors.username = "Логин: 3–64 символа, только латиница, цифры и . _ -";
  }

  if (displayName === "") errors.display_name = "Укажите имя оператора";

  if (input.password === "") {
    errors.password = "Укажите пароль";
  } else if (input.password.length < 8) {
    errors.password = "Пароль должен содержать минимум 8 символов";
  } else if (input.password.length > 128) {
    errors.password = "Пароль не должен превышать 128 символов";
  }

  if (!ADMIN_ROLES.includes(input.role)) errors.role = "Выберите роль";

  if (input.max_chats !== null) {
    if (!Number.isFinite(input.max_chats) || input.max_chats < 0) {
      errors.max_chats = "Лимит чатов должен быть неотрицательным числом";
    } else if (input.max_chats > MAX_CHATS_LIMIT) {
      errors.max_chats = `Лимит чатов не должен превышать ${MAX_CHATS_LIMIT}`;
    }
  }

  return errors;
}

export function validateOperatorUpdate(input: OperatorUpdateInput): FieldErrors {
  const errors: FieldErrors = {};
  if (input.display_name.trim() === "") errors.display_name = "Укажите имя оператора";
  if (!ADMIN_ROLES.includes(input.role)) errors.role = "Выберите роль";

  if (input.max_chats !== null) {
    if (!Number.isFinite(input.max_chats) || input.max_chats < 0) {
      errors.max_chats = "Лимит чатов должен быть неотрицательным числом";
    } else if (input.max_chats > MAX_CHATS_LIMIT) {
      errors.max_chats = `Лимит чатов не должен превышать ${MAX_CHATS_LIMIT}`;
    }
  }

  return errors;
}

export function validatePassword(password: string): string | null {
  if (password === "") return "Укажите новый пароль";
  if (password.length < 8) return "Пароль должен содержать минимум 8 символов";
  if (password.length > 128) return "Пароль не должен превышать 128 символов";
  return null;
}

export function validateAccountCreate(input: AccountCreateInput): FieldErrors {
  const errors: FieldErrors = {};
  const label = input.label.trim();

  if (label === "") {
    errors.label = "Укажите метку аккаунта";
  } else if (label.length > 64) {
    errors.label = "Метка не должна превышать 64 символа";
  }

  if (input.tg_username !== null && input.tg_username.length > 64) {
    errors.tg_username = "Username не должен превышать 64 символа";
  }
  if (input.phone !== null && input.phone.length > 32) {
    errors.phone = "Телефон не должен превышать 32 символа";
  }
  if (input.api_id !== null && (!Number.isInteger(input.api_id) || input.api_id < 1)) {
    errors.api_id = "api_id должен быть целым положительным числом";
  }
  if (input.api_hash !== null && input.api_hash.length > 64) {
    errors.api_hash = "api_hash не должен превышать 64 символа";
  }

  return errors;
}

export function validateAccountUpdate(input: AccountUpdateInput): FieldErrors {
  const errors = validateAccountCreate({
    label: input.label,
    tg_username: input.tg_username,
    phone: input.phone,
    api_id: input.api_id,
    api_hash: input.api_hash,
    session_string: input.session_string,
    is_active: input.is_active,
  });
  return errors;
}

export function hasFieldErrors(errors: FieldErrors): boolean {
  return Object.keys(errors).length > 0;
}

/**
 * Проверка `sla_target_seconds` до отправки: диапазон совпадает с `CHECK`
 * бэкенда (60..86400), чтобы пользователь видел ошибку рядом с полем,
 * а не 422 от сервера.
 */
export function validateSettingsTarget(seconds: number): string | null {
  if (!Number.isInteger(seconds)) return "Введите целое число секунд";
  if (seconds < SLA_TARGET_SECONDS_MIN) {
    return `Минимум ${SLA_TARGET_SECONDS_MIN} секунд (1 минута)`;
  }
  if (seconds > SLA_TARGET_SECONDS_MAX) {
    return `Максимум ${SLA_TARGET_SECONDS_MAX} секунд (24 часа)`;
  }
  return null;
}

/* ------------------------------------------------------------------ store */

interface AdminState {
  /* session */
  token: string | null;
  operator: AdminOperator | null;
  loginLoading: boolean;
  loginError: string | null;

  /* operators */
  operators: AdminOperator[];
  operatorsLoading: boolean;
  operatorsLoaded: boolean;
  operatorsError: string | null;

  /* telegram accounts */
  accounts: AdminAccount[];
  accountsLoading: boolean;
  accountsLoaded: boolean;
  accountsError: string | null;

  /* audit journal */
  audit: AuditEntry[];
  auditLoading: boolean;
  auditLoaded: boolean;
  auditError: string | null;

  /* global SLA settings */
  settings: AdminSettings | null;
  settingsLoading: boolean;
  settingsLoaded: boolean;
  settingsError: string | null;
  settingsSaving: boolean;
  /** Короткое подтверждение внизу вкладки: «Сохранено» / «Изменения отменены». */
  settingsToast: string | null;

  /* mutations */
  submitting: boolean;
  mutationError: string | null;

  /* actions */
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  loadOperators: (force?: boolean) => Promise<void>;
  createOperator: (input: OperatorCreateInput) => Promise<AdminOperator | null>;
  updateOperator: (id: number, input: OperatorUpdateInput) => Promise<AdminOperator | null>;
  deactivateOperator: (id: number) => Promise<boolean>;
  resetPassword: (id: number, newPassword: string) => Promise<boolean>;
  loadAccounts: (force?: boolean) => Promise<void>;
  createAccount: (input: AccountCreateInput) => Promise<AdminAccount | null>;
  updateAccount: (id: number, input: AccountUpdateInput) => Promise<AdminAccount | null>;
  deleteAccount: (id: number) => Promise<boolean>;
  loadAudit: (force?: boolean) => Promise<void>;
  loadSettings: (force?: boolean) => Promise<void>;
  saveSettings: (patch: AdminSettingsPatch) => Promise<AdminSettings | null>;
  /** Показать/сбросить короткий тост вкладки SLA (`null` — скрыть). */
  setSettingsToast: (message: string | null) => void;
  clearError: () => void;
}

/**
 * Дедупликатор запросов: пока промис летит, повторный вызов возвращает тот же
 * промис. Это и «мьютекс» из ТЗ, и экономия сети при двойном монтировании
 * компонента в StrictMode.
 */
const inflight = new Map<string, Promise<unknown>>();

function once<T>(key: string, task: () => Promise<T>): Promise<T> {
  const existing = inflight.get(key);
  if (existing) return existing as Promise<T>;
  const promise = task().finally(() => {
    inflight.delete(key);
  });
  inflight.set(key, promise);
  return promise;
}

/** Сессия отозвана сервером: чистим локальное состояние. */
function signedOutPatch(): Partial<AdminState> {
  return {
    token: null,
    operator: null,
    operators: [],
    operatorsLoaded: false,
    accounts: [],
    accountsLoaded: false,
    audit: [],
    auditLoaded: false,
    settings: null,
    settingsLoaded: false,
    submitting: false,
    loginLoading: false,
  };
}

/** Сброс вкладки SLA при смене/истечении сессии. */
function clearedSettingsPatch(): Partial<AdminState> {
  return {
    settings: null,
    settingsLoading: false,
    settingsLoaded: false,
    settingsError: null,
    settingsSaving: false,
    settingsToast: null,
  };
}

const initialToken = readStoredToken();
const initialOperator = initialToken ? readStoredOperator() : null;

export const useAdminStore = create<AdminState>((set, get) => {
  /**
   * Общий обработчик ошибок. Возвращает `true`, если сессия была сброшена
   * (401/403) — вызывающему коду в этом случае ничего обновлять не нужно.
   */
  function handleFailure(error: unknown, errorKey: "loginError" | "operatorsError" | "accountsError" | "auditError" | "settingsError" | "mutationError"): boolean {
    if (error instanceof AdminApiError && error.isAuthError) {
      clearStoredSession();
      set({ ...signedOutPatch(), ...clearedSettingsPatch(), loginError: null });
      if (errorKey !== "loginError") {
        set({ [errorKey]: "Сессия истекла — войдите заново" } as Pick<AdminState, typeof errorKey>);
      }
      return true;
    }
    if (errorKey === "loginError") {
      set({ loginError: normalizeError(error), loginLoading: false });
    } else {
      set({ [errorKey]: normalizeError(error) } as Pick<AdminState, typeof errorKey>);
    }
    return false;
  }

  function requireToken(): string | null {
    const { token, operator } = get();
    if (!token || !operator) {
      clearStoredSession();
      set(signedOutPatch());
      return null;
    }
    return token;
  }

  return {
    /* ---------------------------------------------------------- session */

    token: initialToken,    operator: initialOperator,
    loginLoading: false,
    loginError: null,

    operators: [],
    operatorsLoading: false,
    operatorsLoaded: false,
    operatorsError: null,

    accounts: [],
    accountsLoading: false,
    accountsLoaded: false,
    accountsError: null,

    audit: [],
    auditLoading: false,
    auditLoaded: false,
    auditError: null,

    settings: null,
    settingsLoading: false,
    settingsLoaded: false,
    settingsError: null,
    settingsSaving: false,
    settingsToast: null,

    submitting: false,
    mutationError: null,

    login: async (username, password) => {
      if (get().loginLoading) return false;
      const trimmed = username.trim();
      if (trimmed === "" || password === "") {
        set({ loginError: "Введите логин и пароль", loginLoading: false });
        return false;
      }

      set({ loginLoading: true, loginError: null });
      try {
        const payload = await http<unknown>("/api/admin/login", {
          method: "POST",
          body: { username: trimmed, password },
        });
        const session = normalizeLogin(payload);
        if (!session) {
          set({ loginLoading: false, loginError: "Сервер вернул некорректный ответ при входе" });
          return false;
        }

        persistSession(session.access_token, session.operator);
        set({
          token: session.access_token,
          operator: session.operator,
          loginLoading: false,
          loginError: null,
          /* новый администратор — свежие данные на всех вкладках */
          operators: [],
          operatorsLoaded: false,
          operatorsError: null,
          accounts: [],
          accountsLoaded: false,
          accountsError: null,
          audit: [],
          auditLoaded: false,
          auditError: null,
          settings: null,
          settingsLoading: false,
          settingsLoaded: false,
          settingsError: null,
          settingsSaving: false,
          settingsToast: null,
          mutationError: null,
          submitting: false,
        });
        return true;
      } catch (error) {
        const status = error instanceof AdminApiError ? error.status : 0;
        let message = normalizeError(error);
        if (status === 403) message = humanizeDetail(ADMIN_REQUIRED_TEXT) ?? ADMIN_REQUIRED_TEXT;
        set({ loginLoading: false, loginError: message });
        return false;
      }
    },

    /**
     * Выход. Admin-токен живёт только на клиенте (`scope="admin"`), поэтому
     * выход — это безусловная локальная зачистка: даже при упавшей сети
     * администратор обязан оказаться вне панели. Отзыв токена на сервере
     * не вызывается — это деск-эндпоинт `/api/auth/logout`.
     */
    logout: async () => {
      clearStoredSession();
      inflight.clear();
      set(signedOutPatch());
      set({ ...clearedSettingsPatch(), loginError: null, mutationError: null });
    },

    /* -------------------------------------------------------- operators */

    loadOperators: async (force = false) =>
      once("operators", async () => {
        const { operatorsLoaded, operatorsLoading } = get();
        if (operatorsLoading) return;
        if (operatorsLoaded && !force) return;
        const token = requireToken();
        if (!token) return;

        set({ operatorsLoading: true, operatorsError: null });
        try {
          const payload = await http<unknown>("/api/admin/operators", { token });
          set({
            operators: normalizeList(payload, normalizeOperator),
            operatorsLoading: false,
            operatorsLoaded: true,
          });
        } catch (error) {
          set({ operatorsLoading: false });
          handleFailure(error, "operatorsError");
        }
      }),

    createOperator: async (input) =>
      once("operator:create", async () => {
        const token = requireToken();
        if (!token) return null;

        set({ submitting: true, mutationError: null });
        try {
          const payload = await http<unknown>("/api/admin/operators", {
            method: "POST",
            token,
            body: {
              username: input.username.trim(),
              display_name: input.display_name.trim(),
              password: input.password,
              role: input.role,
              is_active: input.is_active,
              max_chats: input.max_chats,
              can_delete_messages: input.can_delete_messages,
              can_export: input.can_export,
              avatar_color: input.avatar_color,
            },
          });
          const created = normalizeOperator(payload);
          if (!created) {
            set({ submitting: false, mutationError: "Сервер вернул некорректный ответ" });
            return null;
          }
          const current = get().operators;
          const withoutDuplicate = current.filter((item) => item.id !== created.id);
          set({
            operators: [...withoutDuplicate, created].sort((left, right) => left.id - right.id),
            operatorsLoaded: true,
            submitting: false,
            mutationError: null,
          });
          return created;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return null;
        }
      }),

    updateOperator: async (id, input) =>
      once(`operator:update:${id}`, async () => {
        const token = requireToken();
        if (!token) return null;

        set({ submitting: true, mutationError: null });
        try {
          const payload = await http<unknown>(`/api/admin/operators/${id}`, {
            method: "PATCH",
            token,
            body: {
              display_name: input.display_name.trim(),
              role: input.role,
              is_active: input.is_active,
              max_chats: input.max_chats,
              can_delete_messages: input.can_delete_messages,
              can_export: input.can_export,
              avatar_color: input.avatar_color,
            },
          });
          const updated = normalizeOperator(payload);
          if (!updated) {
            set({ submitting: false, mutationError: "Сервер вернул некорректный ответ" });
            return null;
          }
          set({
            operators: get().operators.map((item) => (item.id === updated.id ? updated : item)),
            submitting: false,
            mutationError: null,
          });
          return updated;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return null;
        }
      }),

    deactivateOperator: async (id) =>
      once(`operator:delete:${id}`, async () => {
        const token = requireToken();
        if (!token) return false;

        set({ submitting: true, mutationError: null });
        try {
          const payload = await http<unknown>(`/api/admin/operators/${id}`, {
            method: "DELETE",
            token,
          });
          const updated = normalizeOperator(payload);
          set({
            operators: get().operators.map((item) =>
              updated && item.id === updated.id
                ? updated
                : item.id === id
                  ? { ...item, is_active: false }
                  : item,
            ),
            submitting: false,
            mutationError: null,
          });
          return true;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return false;
        }
      }),

    resetPassword: async (id, newPassword) =>
      once(`operator:password:${id}`, async () => {
        const token = requireToken();
        if (!token) return false;

        const validationError = validatePassword(newPassword);
        if (validationError) {
          set({ mutationError: validationError });
          return false;
        }

        set({ submitting: true, mutationError: null });
        try {
          await http<unknown>(`/api/admin/operators/${id}/reset-password`, {
            method: "POST",
            token,
            body: { new_password: newPassword },
          });
          set({ submitting: false, mutationError: null });
          return true;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return false;
        }
      }),

    /* --------------------------------------------------------- accounts */

    loadAccounts: async (force = false) =>
      once("accounts", async () => {
        const { accountsLoaded, accountsLoading } = get();
        if (accountsLoading) return;
        if (accountsLoaded && !force) return;
        const token = requireToken();
        if (!token) return;

        set({ accountsLoading: true, accountsError: null });
        try {
          const payload = await http<unknown>("/api/admin/accounts", { token });
          set({
            accounts: normalizeList(payload, normalizeAccount),
            accountsLoading: false,
            accountsLoaded: true,
          });
        } catch (error) {
          set({ accountsLoading: false });
          handleFailure(error, "accountsError");
        }
      }),

    createAccount: async (input) =>
      once("account:create", async () => {
        const token = requireToken();
        if (!token) return null;

        set({ submitting: true, mutationError: null });
        try {
          const payload = await http<unknown>("/api/admin/accounts", {
            method: "POST",
            token,
            body: {
              label: input.label.trim(),
              tg_username: input.tg_username,
              phone: input.phone,
              api_id: input.api_id,
              api_hash: input.api_hash,
              session_string: input.session_string,
              is_active: input.is_active,
            },
          });
          const created = normalizeAccount(payload);
          if (!created) {
            set({ submitting: false, mutationError: "Сервер вернул некорректный ответ" });
            return null;
          }
          const current = get().accounts;
          const withoutDuplicate = current.filter((item) => item.id !== created.id);
          set({
            accounts: [...withoutDuplicate, created].sort((left, right) => left.id - right.id),
            accountsLoaded: true,
            submitting: false,
            mutationError: null,
          });
          return created;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return null;
        }
      }),

    updateAccount: async (id, input) =>
      once(`account:update:${id}`, async () => {
        const token = requireToken();
        if (!token) return null;

        set({ submitting: true, mutationError: null });
        try {
          /* Секреты уходят только когда оператор явно ввёл новые значения:
             сервер их не возвращает, и `null` в теле стёр бы рабочий аккаунт. */
          const body: Record<string, unknown> = {
            label: input.label.trim(),
            tg_username: input.tg_username,
            phone: input.phone,
            api_id: input.api_id,
            is_active: input.is_active,
          };
          if (input.api_hash !== null && input.api_hash !== "") body.api_hash = input.api_hash;
          if (input.session_string !== null && input.session_string !== "") {
            body.session_string = input.session_string;
          }

          const payload = await http<unknown>(`/api/admin/accounts/${id}`, {
            method: "PATCH",
            token,
            body,
          });
          const updated = normalizeAccount(payload);
          if (!updated) {
            set({ submitting: false, mutationError: "Сервер вернул некорректный ответ" });
            return null;
          }
          set({
            accounts: get().accounts.map((item) => (item.id === updated.id ? updated : item)),
            submitting: false,
            mutationError: null,
          });
          return updated;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return null;
        }
      }),

    deleteAccount: async (id) =>
      once(`account:delete:${id}`, async () => {
        const token = requireToken();
        if (!token) return false;

        set({ submitting: true, mutationError: null });
        try {
          await http<unknown>(`/api/admin/accounts/${id}`, { method: "DELETE", token });
          set({
            accounts: get().accounts.filter((item) => item.id !== id),
            submitting: false,
            mutationError: null,
          });
          return true;
        } catch (error) {
          set({ submitting: false });
          handleFailure(error, "mutationError");
          return false;
        }
      }),

    /* ------------------------------------------------------------ audit */

    loadAudit: async (force = false) =>
      once("audit", async () => {
        const { auditLoaded, auditLoading } = get();
        if (auditLoading) return;
        if (auditLoaded && !force) return;
        const token = requireToken();
        if (!token) return;

        set({ auditLoading: true, auditError: null });
        try {
          const payload = await http<unknown>(`/api/admin/audit?limit=${AUDIT_LIMIT}`, { token });
          set({
            audit: normalizeList(payload, normalizeAuditEntry),
            auditLoading: false,
            auditLoaded: true,
          });
        } catch (error) {
          set({ auditLoading: false });
          handleFailure(error, "auditError");
        }
      }),

    /* --------------------------------------------------------- settings */

    /**
     * `GET /api/admin/settings` — настройки SLA (CONTRACTS_V5 §2).
     *
     * `force=false` отдаёт закэшированное значение и не дёргает сеть при каждом
     * переключении вкладки; `true` (кнопка «Обновить») перечитывает сервер.
     */
    loadSettings: async (force = false) =>
      once("settings", async () => {
        const { settingsLoaded, settingsLoading } = get();
        if (settingsLoading) return;
        if (settingsLoaded && !force) return;
        const token = requireToken();
        if (!token) return;

        set({ settingsLoading: true, settingsError: null, settingsToast: null });
        try {
          const payload = await http<unknown>("/api/admin/settings", { token });
          const loaded = normalizeSettings(payload);
          if (!loaded) {
            set({
              settingsLoading: false,
              settingsLoaded: true,
              settingsError: "Сервер вернул некорректный ответ",
            });
            return;
          }
          set({
            settings: loaded,
            settingsLoading: false,
            settingsLoaded: true,
            settingsError: null,
          });
        } catch (error) {
          set({ settingsLoading: false });
          handleFailure(error, "settingsError");
        }
      }),

    /**
     * `PATCH /api/admin/settings` — сохранение только изменённых полей.
     *
     * Локальный стейт обновляется **из ответа сервера** (а не из отправленного
     * патча): `updated_at`/`updated_by` и клампинг `sla_target_seconds` знает
     * только бэкенд. Успех подтверждается тостом вкладки.
     */
    saveSettings: async (patch) => {
      const token = requireToken();
      if (!token) return null;

      if (Object.keys(patch).length === 0) return get().settings;

      /*
       * Без `once()`: этот флаг — «мьютекс» кнопки, а не дедупликатор.
       * `once()` вернул бы второй вызов, нажатый, пока окно вкладки было фоном:
       * браузер держит `fetch` до фокуса, запрос выглядит «висящим», и
       * повторное сохранение получало бы чужой промис вместо своего PATCH.
       */
      if (get().settingsSaving) return null;

      set({ settingsSaving: true, settingsError: null, settingsToast: null });
      try {
        const payload = await http<unknown>("/api/admin/settings", {
          method: "PATCH",
          token,
          body: patch,
        });
        const saved = normalizeSettings(payload);
        if (!saved) {
          set({
            settingsSaving: false,
            settingsError: "Сервер вернул некорректный ответ",
          });
          return null;
        }
        set({
          settings: saved,
          settingsLoaded: true,
          settingsSaving: false,
          settingsError: null,
          settingsToast: "Настройки SLA сохранены",
        });
        return saved;
      } catch (error) {
        set({ settingsSaving: false });
        handleFailure(error, "settingsError");
        return null;
      }
    },

    setSettingsToast: (message) => set({ settingsToast: message }),

    /* ------------------------------------------------------------ misc */

    clearError: () => set({ loginError: null, mutationError: null }),
  };
});
