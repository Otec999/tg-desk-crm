/**
 * Админ-панель TG-Desk — CONTRACTS_V2.md §3.3 / §6, файл A9.
 *
 * Четыре вкладки без внешнего роутера: «Операторы», «Telegram-аккаунты»,
 * «Журнал действий» и «SLA» (глобальные настройки, CONTRACTS_V5 §2). Данные
 * активной вкладки грузятся при монтировании (и повторно при переключении, если
 * кэш пуст), все мутации идут через `useAdminStore`.
 *
 * Селекторы Zustand возвращают **стабильные ссылки** на поля стора — никаких
 * `.filter()`/`.map()`/литералов внутри селектора, иначе React уйдёт в
 * бесконечный ререндер (грабли, на которые этот проект уже наступал).
 *
 * Роли: сервер пускает в панель ADMIN и SUPERVISOR, но **все** мутации закрыты
 * `require_admin` (403). Поэтому каждый контрол изменения получает
 * `readOnly={isSupervisor}` и рисуется disabled — лучше честно погасить
 * контрол, чем ловить 403 после клика.
 */

import {
  AlertCircle,
  Ban,
  CheckCircle2,
  ChevronLeft,
  Database,
  Eye,
  EyeOff,
  History,
  KeyRound,
  Loader2,
  LogOut,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  ScrollText,
  ShieldCheck,
  Timer,
  TimerReset,
  Trash2,
  UserCog,
  Users,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";

import { avatarColor as seedColor, initials } from "../lib/format";
import {
  ADMIN_ROLES,
  hasFieldErrors,
  SLA_TARGET_SECONDS_MAX,
  SLA_TARGET_SECONDS_MIN,
  useAdminStore,
  validateAccountCreate,
  validateAccountUpdate,
  validateOperatorCreate,
  validateOperatorUpdate,
  validatePassword,
  validateSettingsTarget,
  type AccountCreateInput,
  type AccountUpdateInput,
  type AdminAccount,
  type AdminOperator,
  type AdminRole,
  type AdminSettings,
  type AdminSettingsPatch,
  type AuditEntry,
  type FieldErrors,
  type OperatorCreateInput,
  type OperatorUpdateInput,
} from "../store/useAdminStore";

/* ================================================================== meta */

type TabKey = "operators" | "accounts" | "audit" | "settings";

const LOGIN_PATH = "/admin";
const DESK_PATH = "/";

const TABS: ReadonlyArray<{ key: TabKey; label: string; icon: LucideIcon }> = [
  { key: "operators", label: "Операторы", icon: Users },
  { key: "accounts", label: "Telegram-аккаунты", icon: Database },
  { key: "settings", label: "SLA", icon: Timer },
  { key: "audit", label: "Журнал действий", icon: History },
];

const ROLE_BADGE: Record<string, string> = {
  ADMIN: "border-tg-accent/50 bg-tg-accent/15 text-tg-accentHover",
  SUPERVISOR: "border-tg-warning/50 bg-tg-warning/10 text-tg-warning",
  OPERATOR: "border-tg-border bg-tg-elevated text-tg-textMuted",
};

const ROLE_LABEL: Record<string, string> = {
  ADMIN: "ADMIN",
  SUPERVISOR: "SUPERVISOR",
  OPERATOR: "OPERATOR",
};

const SHIFT_META: Record<string, { label: string; dot: string; text: string }> = {
  ONLINE: { label: "Онлайн", dot: "bg-tg-online", text: "text-tg-online" },
  PAUSE: { label: "Пауза", dot: "bg-tg-pause", text: "text-tg-pause" },
  OFFLINE: { label: "Офлайн", dot: "bg-tg-offline", text: "text-tg-offline" },
};

const AUDIT_ACTION_LABEL: Record<string, string> = {
  create_operator: "Создание оператора",
  update_operator: "Изменение оператора",
  deactivate_operator: "Деактивация",
  reset_password: "Сброс пароля",
  create_account: "Добавление аккаунта",
  update_account: "Изменение аккаунта",
  delete_account: "Удаление аккаунта",
  admin_login: "Вход администратора",
  admin_login_failed: "Неудачный вход",
  admin_logout: "Выход",
};

const TARGET_TYPE_LABEL: Record<string, string> = {
  operator: "оператор",
  telegram_account: "Telegram-аккаунт",
};

/* =============================================================== helpers */

function roleOf(value: string): AdminRole {
  const upper = value.toUpperCase();
  return (ADMIN_ROLES as readonly string[]).includes(upper) ? (upper as AdminRole) : "OPERATOR";
}

function shiftOf(value: string): { label: string; dot: string; text: string } {
  return SHIFT_META[value.toUpperCase()] ?? SHIFT_META.OFFLINE;
}

function actionLabel(action: string): string {
  return AUDIT_ACTION_LABEL[action] ?? "Действие";
}

function targetLabel(entry: AuditEntry): string {
  if (!entry.target_type && entry.target_id === null) return "—";
  const type = entry.target_type ? (TARGET_TYPE_LABEL[entry.target_type] ?? entry.target_type) : "запись";
  return entry.target_id === null ? type : `${type} #${entry.target_id}`;
}

/** «21.10.2026 14:32» — фиксированный формат, не зависящий от локали машины. */
function formatAuditTime(value: string | null): string {
  if (!value) return "—";
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (part: number): string => String(part).padStart(2, "0");
  return (
    `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function operatorAvatarColor(operator: AdminOperator): string {
  return operator.avatar_color ?? seedColor(operator.username);
}

function parseOptionalInt(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return Number.NaN;
  return Number.parseInt(trimmed, 10);
}

function parseOptionalText(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * «30 минут», «1 час 30 минут», «45 секунд» — человекочитаемая подсказка
 * рядом с полем «Цель ответа, секунд». Нули опускаются, чтобы «7200» читалось
 * как «2 часа», а не «2 часа 0 минут».
 */
function humanizeSeconds(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return "—";
  const whole = Math.round(totalSeconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = whole % 60;

  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours} ${plural(hours, "час", "часа", "часов")}`);
  if (minutes > 0) parts.push(`${minutes} ${plural(minutes, "минута", "минуты", "минут")}`);
  if (seconds > 0) parts.push(`${seconds} ${plural(seconds, "секунда", "секунды", "секунд")}`);
  return parts.join(" ") || "0 секунд";
}

/** Русское склонение: 1 минута / 2 минуты / 5 минут. */
function plural(value: number, one: string, few: string, many: string): string {
  const mod100 = Math.abs(value) % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = mod100 % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

/* ============================================================ primitives */

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <label className="block">
      <span className="mb-1 block text-2xs font-semibold uppercase tracking-wider text-tg-textFaint">{label}</span>
      {children}
      {error ? (
        <span className="mt-1 block text-2xs text-tg-danger">{error}</span>
      ) : hint ? (
        <span className="mt-1 block text-2xs text-tg-textFaint">{hint}</span>
      ) : null}
    </label>
  );
}

const INPUT_CLASS =
  "w-full rounded-lg border border-tg-border bg-tg-elevated px-2.5 py-2 text-xs text-tg-text placeholder:text-tg-textFaint transition focus:border-tg-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60";

const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function TextInput(
  { className, ...rest },
  ref,
): JSX.Element {
  return <input ref={ref} {...rest} className={`${INPUT_CLASS} ${className ?? ""}`} />;
});

function TextArea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>): JSX.Element {
  return <textarea {...rest} className={`${INPUT_CLASS} resize-y font-mono leading-relaxed ${className ?? ""}`} />;
}

function Checkbox({
  checked,
  onChange,
  label,
  disabled,
  readOnly,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
  /** Роль без прав на запись (SUPERVISOR): контрол виден, но погашен. */
  readOnly?: boolean;
}): JSX.Element {
  const blocked = disabled || readOnly;
  return (
    <label className={`flex select-none items-center gap-2 text-xs text-tg-text ${blocked ? "" : "cursor-pointer"}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={blocked}
        aria-label={label}
        onChange={(event) => onChange(event.target.checked)}
        className="h-3.5 w-3.5 shrink-0 cursor-pointer appearance-none rounded border border-tg-border bg-tg-elevated transition checked:border-tg-accent checked:bg-tg-accent focus:outline-none focus-visible:ring-1 focus-visible:ring-tg-accent/60 disabled:cursor-not-allowed disabled:opacity-60"
      />
      <span className={blocked ? "text-tg-textMuted" : undefined}>{label}</span>
    </label>
  );
}

function Toggle({
  checked,
  disabled,
  readOnly,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  /** Роль без прав на запись (SUPERVISOR): переключатель виден, но погашен. */
  readOnly?: boolean;
  label: string;
  onChange: (value: boolean) => void;
}): JSX.Element {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={readOnly ? `${label} — недоступно в режиме супервизора` : label}
      disabled={disabled || readOnly}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-4 w-8 shrink-0 items-center rounded-full border transition disabled:cursor-not-allowed disabled:opacity-50 ${
        checked ? "border-tg-accent bg-tg-accent" : "border-tg-border bg-tg-elevated"
      }`}
    >
      <span
        className={`absolute h-2.5 w-2.5 rounded-full bg-white transition-all ${checked ? "left-[1.05rem]" : "left-[0.2rem]"}`}
      />
    </button>
  );
}

function RoleBadge({ role }: { role: string }): JSX.Element {
  const key = role.toUpperCase();
  return (
    <span
      className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${
        ROLE_BADGE[key] ?? ROLE_BADGE.OPERATOR
      }`}
    >
      {ROLE_LABEL[key] ?? key}
    </span>
  );
}

function SkeletonRows({ rows = 5 }: { rows?: number }): JSX.Element {
  return (
    <div className="space-y-2 p-4">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="relative overflow-hidden rounded-lg border border-tg-borderSoft bg-tg-panelAlt">
          <div className="flex items-center gap-3 px-3 py-3">
            <div className="h-8 w-8 shrink-0 rounded-full bg-tg-hover" />
            <div className="flex-1 space-y-1.5">
              <div className="h-2 w-1/3 rounded bg-tg-hover" />
              <div className="h-2 w-2/3 rounded bg-tg-hover/60" />
            </div>
          </div>
          <span className="pointer-events-none absolute inset-y-0 -left-full w-full animate-shimmer bg-gradient-to-r from-transparent via-white/[0.045] to-transparent" />
        </div>
      ))}
    </div>
  );
}

function EmptyState({
  icon: Icon,
  title,
  hint,
}: {
  icon: LucideIcon;
  title: string;
  hint?: string;
}): JSX.Element {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-full bg-tg-elevated text-tg-textFaint">
        <Icon size={20} />
      </div>
      <p className="text-sm font-medium text-tg-textMuted">{title}</p>
      {hint ? <p className="max-w-sm text-2xs text-tg-textFaint">{hint}</p> : null}
    </div>
  );
}

function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }): JSX.Element {
  return (
    <div className="mx-4 mt-4 flex items-start gap-2 rounded-lg border border-tg-danger/40 bg-tg-danger/10 px-3 py-2 text-xs text-tg-danger">
      <AlertCircle size={14} className="mt-[1px] shrink-0" />
      <span className="flex-1">{message}</span>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded border border-tg-danger/40 px-1.5 py-0.5 text-2xs font-semibold transition hover:bg-tg-danger/20"
        >
          Повторить
        </button>
      ) : null}
    </div>
  );
}

function Modal({
  title,
  subtitle,
  onClose,
  children,
  footer,
  wide,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer: ReactNode;
  wide?: boolean;
}): JSX.Element {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={`my-auto w-full ${wide ? "max-w-2xl" : "max-w-lg"} animate-slide-up overflow-hidden rounded-2xl border border-tg-border bg-tg-panel shadow-pop`}
      >
        <div className="flex items-start gap-3 border-b border-tg-border px-5 py-3.5">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-semibold text-tg-text">{title}</h2>
            {subtitle ? <p className="mt-0.5 truncate text-2xs text-tg-textFaint">{subtitle}</p> : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть"
            className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text"
          >
            <X size={16} />
          </button>
        </div>
        <div className="max-h-[65vh] overflow-y-auto px-5 py-4">{children}</div>
        <div className="flex items-center justify-end gap-2 border-t border-tg-border bg-tg-panelAlt px-5 py-3">{footer}</div>
      </div>
    </div>
  );
}

function PrimaryButton({
  children,
  onClick,
  disabled,
  loading,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  loading?: boolean;
  title?: string;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-tg-accent px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-tg-accentHover disabled:cursor-not-allowed disabled:opacity-60"
    >
      {loading ? <Loader2 size={13} className="animate-spin" /> : null}
      {children}
    </button>
  );
}

function GhostButton({
  children,
  onClick,
  disabled,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="inline-flex items-center justify-center gap-1 rounded-lg border border-tg-border bg-tg-elevated px-3 py-1.5 text-xs font-medium text-tg-textMuted transition hover:border-tg-hover hover:text-tg-text disabled:cursor-not-allowed disabled:opacity-60"
    >
      {children}
    </button>
  );
}

/* ============================================================ SLA settings */

/** Черновик формы: секунды держим строкой, чтобы поле можно было очистить. */
interface SettingsFormState {
  sla_enabled: boolean;
  sla_target_seconds: string;
  sla_reset_on_operator_reply: boolean;
}

function settingsToForm(settings: AdminSettings): SettingsFormState {
  return {
    sla_enabled: settings.sla_enabled,
    sla_target_seconds: String(settings.sla_target_seconds),
    sla_reset_on_operator_reply: settings.sla_reset_on_operator_reply,
  };
}

/** «admin», «система» — кто последним сохранял настройки. */
function formatSettingsAuthor(settings: AdminSettings): string {
  return settings.updated_by ?? "система (значения по умолчанию)";
}

/**
 * Вкладка «SLA» — глобальные настройки (CONTRACTS_V5 §2):
 * `sla_enabled`, `sla_target_seconds`, `sla_reset_on_operator_reply`.
 *
 * Кнопка «Сохранить» активна только при реальных изменениях и ненулевом
 * диффе; `PATCH` получает ровно изменённые поля. Для SUPERVISOR все контролы
 * погашены (`readOnly`), потому что `PATCH /api/admin/settings` — admin-only.
 */
function SettingsTab({
  settings,
  loading,
  error,
  saving,
  toast,
  readOnly,
  onReload,
  onSave,
}: {
  settings: AdminSettings | null;
  loading: boolean;
  error: string | null;
  saving: boolean;
  toast: string | null;
  readOnly: boolean;
  onReload: () => void;
  onSave: (patch: AdminSettingsPatch) => void;
}): JSX.Element {
  const [form, setForm] = useState<SettingsFormState | null>(settings ? settingsToForm(settings) : null);

  /**
   * Пересобираем черновик из стора: загрузка, сохранение (ответ сервера с
   * нормализованными полями) и откат «Отменить изменения». Пока пользователь
   * печатает, `settings` не меняется, поэтому ввод не затирается.
   */
  useEffect(() => {
    if (settings) setForm(settingsToForm(settings));
  }, [settings]);

  const targetSeconds = form === null ? Number.NaN : Number(form.sla_target_seconds.trim());
  const targetError =
    form === null || form.sla_target_seconds.trim() === ""
      ? null
      : validateSettingsTarget(targetSeconds);

  const draft: AdminSettingsPatch | null =
    form === null
      ? null
      : {
          sla_enabled: form.sla_enabled,
          sla_target_seconds: Number.isFinite(targetSeconds) ? targetSeconds : Number.NaN,
          sla_reset_on_operator_reply: form.sla_reset_on_operator_reply,
        };

  /** Только реально изменённые поля — бэкенд ничего лишнего не перезаписывает. */
  const patch = useMemo<AdminSettingsPatch>(() => {
    if (!settings || !draft) return {};
    const changed: AdminSettingsPatch = {};
    if (draft.sla_enabled !== undefined && draft.sla_enabled !== settings.sla_enabled) {
      changed.sla_enabled = draft.sla_enabled;
    }
    if (
      draft.sla_target_seconds !== undefined &&
      draft.sla_target_seconds !== settings.sla_target_seconds
    ) {
      changed.sla_target_seconds = draft.sla_target_seconds;
    }
    if (
      draft.sla_reset_on_operator_reply !== undefined &&
      draft.sla_reset_on_operator_reply !== settings.sla_reset_on_operator_reply
    ) {
      changed.sla_reset_on_operator_reply = draft.sla_reset_on_operator_reply;
    }
    return changed;
  }, [settings, draft]);

  const hasChanges = Object.keys(patch).length > 0;
  const canSave = !readOnly && !saving && hasChanges && targetError === null;
  const targetLocked = form !== null && !form.sla_enabled;

  const patchForm = useCallback((next: Partial<SettingsFormState>) => {
    setForm((previous) => (previous === null ? previous : { ...previous, ...next }));
  }, []);

  const handleSave = useCallback(() => {
    if (!canSave) return;
    onSave(patch);
  }, [canSave, onSave, patch]);

  const handleReset = useCallback(() => {
    if (settings) setForm(settingsToForm(settings));
  }, [settings]);

  const previewSeconds = Number.isFinite(targetSeconds) ? targetSeconds : null;

  return (
    <section className="flex min-h-0 flex-1 flex-col" aria-label="Настройки SLA">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-tg-border px-4 py-2.5">
        <h2 className="flex items-center gap-1.5 text-xs font-semibold text-tg-textMuted">
          <Timer size={13} />
          Настройки SLA
        </h2>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={onReload}
            disabled={loading || saving}
            title="Обновить настройки"
            aria-label="Обновить настройки SLA"
            className="grid h-7 w-7 place-items-center rounded-lg border border-tg-border bg-tg-elevated text-tg-textMuted transition hover:text-tg-text disabled:opacity-60"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : undefined} />
          </button>
        </div>
      </div>

      {error ? <ErrorBanner message={error} onRetry={onReload} /> : null}

      <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
        {loading && settings === null ? (
          <SkeletonRows rows={3} />
        ) : settings === null || form === null ? (
          <EmptyState
            icon={Timer}
            title="Настройки недоступны"
            hint="Сервер не вернул app_settings. Обновите страницу или проверьте журнал действий."
          />
        ) : (
          <div className="mx-auto w-full max-w-2xl space-y-3">
            <div className="flex items-start gap-3 rounded-lg border border-tg-borderSoft bg-tg-panelAlt px-3 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-tg-text">SLA включён</p>
                <p className="mt-0.5 text-2xs text-tg-textFaint">
                  Выключенный SLA не создаёт новых инцидентов; уже открытые живут своей жизнью
                  и закрываются ответом оператора.
                </p>
              </div>
              <Toggle
                checked={form.sla_enabled}
                disabled={saving}
                readOnly={readOnly}
                label="SLA включён"
                onChange={(value) => patchForm({ sla_enabled: value })}
              />
            </div>

            <div className="rounded-lg border border-tg-borderSoft bg-tg-panelAlt px-3 py-3">
              <Field
                label="Цель ответа, секунд"
                error={targetError ?? undefined}
                hint={
                  targetError ??
                  `От 60 секунд (1 минута) до ${SLA_TARGET_SECONDS_MAX / 3600} часов; ${SLA_TARGET_SECONDS_MIN * 30} секунд — это 30 минут.`
                }
              >
                <div className="flex items-center gap-2">
                  <div className="relative flex-1">
                    <TextInput
                      value={form.sla_target_seconds}
                      onChange={(event) =>
                        patchForm({ sla_target_seconds: event.target.value.replace(/[^\d]/g, "") })
                      }
                      disabled={saving || readOnly}
                      inputMode="numeric"
                      aria-label="Цель ответа, секунд"
                      placeholder={`например ${SLA_TARGET_SECONDS_MIN * 30}`}
                      className="pr-14"
                    />
                    <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-2xs text-tg-textFaint">
                      сек
                    </span>
                  </div>
                  <span
                    className="min-w-[8rem] shrink-0 rounded-md border border-tg-border bg-tg-elevated px-2 py-1.5 text-2xs text-tg-textMuted"
                    aria-live="polite"
                  >
                    {previewSeconds === null ? "—" : humanizeSeconds(previewSeconds)}
                  </span>
                </div>
              </Field>
            </div>

            <div className="flex items-start gap-3 rounded-lg border border-tg-borderSoft bg-tg-panelAlt px-3 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-tg-text">Сбрасывать отсчёт при ответе оператора</p>
                <p className="mt-0.5 text-2xs text-tg-textFaint">
                  Включено: любой ответ CRM закрывает текущий инцидент, и новый клок стартует
                  со следующего входящего. Выключено: закрывает только ответ-цитата.
                </p>
              </div>
              <Toggle
                checked={form.sla_reset_on_operator_reply}
                disabled={saving}
                readOnly={readOnly}
                label="Сбрасывать отсчёт при ответе оператора"
                onChange={(value) => patchForm({ sla_reset_on_operator_reply: value })}
              />
            </div>

            <div className="flex flex-wrap items-center gap-2 border-t border-tg-border pt-3">
              <PrimaryButton
                onClick={handleSave}
                disabled={!canSave}
                loading={saving}
                title={
                  readOnly
                    ? "Только администратор может менять настройки SLA"
                    : hasChanges
                      ? "Сохранить изменения"
                      : "Нет изменений для сохранения"
                }
              >
                <Save size={13} />
                Сохранить
              </PrimaryButton>
              <GhostButton
                onClick={handleReset}
                disabled={!hasChanges || saving || readOnly}
                title="Вернуть значения, загруженные с сервера"
              >
                <TimerReset size={13} />
                Отменить изменения
              </GhostButton>

              <span className="ml-auto flex items-center gap-1.5 text-2xs">
                {saving ? (
                  <span className="flex items-center gap-1 text-tg-textFaint">
                    <Loader2 size={11} className="animate-spin" />
                    Сохранение…
                  </span>
                ) : toast ? (
                  <span className="flex items-center gap-1 text-tg-success" role="status">
                    <CheckCircle2 size={11} />
                    {toast}
                  </span>
                ) : hasChanges ? (
                  <span className="text-tg-warning">Есть несохранённые изменения</span>
                ) : (
                  <span className="text-tg-textFaint">Изменений нет</span>
                )}
              </span>
            </div>

            <div className="space-y-1 rounded-lg border border-tg-borderSoft bg-tg-panelAlt px-3 py-2 text-2xs text-tg-textFaint">
              <p>
                Последнее сохранение:{" "}
                <span className="text-tg-textMuted">{formatAuditTime(settings.updated_at)}</span>
              </p>
              <p>
                Кто сохранил:{" "}
                <span className="font-mono text-tg-textMuted">{formatSettingsAuthor(settings)}</span>
              </p>
              {targetLocked ? (
                <p className="flex items-center gap-1 text-tg-warning">
                  <AlertCircle size={11} />
                  SLA выключен — цель ответа применяется только после включения.
                </p>
              ) : null}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

/* ============================================================ operators */

interface OperatorFormState {
  username: string;
  display_name: string;
  password: string;
  role: AdminRole;
  max_chats: string;
  can_delete_messages: boolean;
  can_export: boolean;
  is_active: boolean;
}

function emptyOperatorForm(): OperatorFormState {
  return {
    username: "",
    display_name: "",
    password: "",
    role: "OPERATOR",
    max_chats: "",
    can_delete_messages: false,
    can_export: false,
    is_active: true,
  };
}

function operatorToForm(operator: AdminOperator): OperatorFormState {
  return {
    username: operator.username,
    display_name: operator.display_name,
    password: "",
    role: roleOf(operator.role),
    max_chats: operator.max_chats === null ? "" : String(operator.max_chats),
    can_delete_messages: operator.can_delete_messages,
    can_export: operator.can_export,
    is_active: operator.is_active,
  };
}

const ROLE_OPTIONS: ReadonlyArray<{ value: AdminRole; label: string; hint: string }> = [
  { value: "OPERATOR", label: "OPERATOR — оператор", hint: "Чаты, ответы клиентам" },
  { value: "SUPERVISOR", label: "SUPERVISOR — супервизор", hint: "Просмотр операторов, без изменений" },
  { value: "ADMIN", label: "ADMIN — администратор", hint: "Полный доступ к панели" },
];

function OperatorModal({
  mode,
  operator,
  submitting,
  serverError,
  onClose,
  onSaved,
  onPasswordReset,
}: {
  mode: "create" | "edit";
  operator: AdminOperator | null;
  submitting: boolean;
  serverError: string | null;
  onClose: () => void;
  onSaved: () => void;
  onPasswordReset: (id: number, password: string) => Promise<boolean>;
}): JSX.Element {
  const createOperator = useAdminStore((state) => state.createOperator);
  const updateOperator = useAdminStore((state) => state.updateOperator);
  const clearError = useAdminStore((state) => state.clearError);

  const [form, setForm] = useState<OperatorFormState>(() =>
    mode === "edit" && operator ? operatorToForm(operator) : emptyOperatorForm(),
  );
  const [errors, setErrors] = useState<FieldErrors>({});
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [passwordVisibleReset, setPasswordVisibleReset] = useState(false);
  const [passwordNote, setPasswordNote] = useState<string | null>(null);
  const [resetBusy, setResetBusy] = useState(false);

  const usernameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (mode === "create") usernameRef.current?.focus();
  }, [mode]);

  const patch = useCallback(
    (changes: Partial<OperatorFormState>) => {
      setForm((previous) => ({ ...previous, ...changes }));
      if (serverError) clearError();
    },
    [clearError, serverError],
  );

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (submitting || resetBusy) return;

    const maxChats = parseOptionalInt(form.max_chats);

    if (mode === "create") {
      const input: OperatorCreateInput = {
        username: form.username.trim(),
        display_name: form.display_name.trim(),
        password: form.password,
        role: form.role,
        max_chats: Number.isNaN(maxChats) ? -1 : maxChats,
        can_delete_messages: form.can_delete_messages,
        can_export: form.can_export,
        is_active: form.is_active,
        avatar_color: null,
      };
      const nextErrors = validateOperatorCreate(input);
      if (hasFieldErrors(nextErrors)) {
        setErrors(nextErrors);
        return;
      }
      setErrors({});
      const created = await createOperator({ ...input, max_chats: maxChats });
      if (created) onSaved();
      return;
    }

    if (!operator) return;

    const input: OperatorUpdateInput = {
      display_name: form.display_name.trim(),
      role: form.role,
      max_chats: Number.isNaN(maxChats) ? -1 : maxChats,
      can_delete_messages: form.can_delete_messages,
      can_export: form.can_export,
      is_active: form.is_active,
      avatar_color: operator.avatar_color,
    };
    const nextErrors = validateOperatorUpdate(input);
    if (hasFieldErrors(nextErrors)) {
      setErrors(nextErrors);
      return;
    }
    setErrors({});
    const updated = await updateOperator(operator.id, { ...input, max_chats: maxChats });
    if (updated) onSaved();
  };

  const onResetPassword = async (): Promise<void> => {
    if (!operator || resetBusy) return;
    const validationError = validatePassword(newPassword);
    if (validationError) {
      setPasswordNote(validationError);
      return;
    }
    setResetBusy(true);
    setPasswordNote(null);
    const ok = await onPasswordReset(operator.id, newPassword);
    setResetBusy(false);
    if (ok) {
      setNewPassword("");
      setPasswordNote("Пароль обновлён");
    } else {
      setPasswordNote(useAdminStore.getState().mutationError ?? "Не удалось сменить пароль");
    }
  };

  return (
    <Modal
      title={mode === "create" ? "Новый оператор" : `Оператор ${operator?.username ?? ""}`}
      subtitle={
        mode === "create"
          ? "Учётная запись для деск-панели"
          : "Логин изменить нельзя — он идентифицирует оператора в истории"
      }
      wide
      onClose={onClose}
      footer={
        <>
          <GhostButton onClick={onClose} disabled={submitting}>
            Отмена
          </GhostButton>
          <button
            type="submit"
            form="operator-form"
            disabled={submitting}
            className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-tg-accent px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-tg-accentHover disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? <Loader2 size={13} className="animate-spin" /> : null}
            {mode === "create" ? "Создать" : "Сохранить"}
          </button>
        </>
      }
    >
      <form id="operator-form" onSubmit={(event) => void onSubmit(event)} noValidate className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Логин" error={errors.username} hint="3–64 символа: A-Z a-z 0-9 . _ -">
            <TextInput
              ref={usernameRef}
              value={form.username}
              onChange={(event) => patch({ username: event.target.value })}
              disabled={mode === "edit" || submitting}
              autoComplete="off"
              spellCheck={false}
              placeholder="operator.one"
            />
          </Field>

          <Field label="Имя" error={errors.display_name}>
            <TextInput
              value={form.display_name}
              onChange={(event) => patch({ display_name: event.target.value })}
              disabled={submitting}
              maxLength={120}
              autoComplete="off"
              placeholder="Иван Петров"
            />
          </Field>
        </div>

        {mode === "create" ? (
          <Field label="Пароль" error={errors.password} hint="Минимум 8 символов">
            <div className="relative">
              <TextInput
                type={passwordVisible ? "text" : "password"}
                value={form.password}
                onChange={(event) => patch({ password: event.target.value })}
                disabled={submitting}
                autoComplete="new-password"
                placeholder="••••••••"
                className="pr-9"
              />
              <button
                type="button"
                onClick={() => setPasswordVisible((previous) => !previous)}
                aria-label={passwordVisible ? "Скрыть пароль" : "Показать пароль"}
                className="absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-text"
              >
                {passwordVisible ? <EyeOff size={13} /> : <Eye size={13} />}
              </button>
            </div>
          </Field>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Роль" error={errors.role}>
            <select
              value={form.role}
              onChange={(event) => patch({ role: roleOf(event.target.value) })}
              disabled={submitting}
              className={INPUT_CLASS}
            >
              {ROLE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Лимит чатов" error={errors.max_chats} hint="Пусто — без ограничения">
            <TextInput
              value={form.max_chats}
              onChange={(event) => patch({ max_chats: event.target.value.replace(/[^\d]/g, "") })}
              disabled={submitting}
              inputMode="numeric"
              placeholder="например 10"
            />
          </Field>
        </div>

        <div className="space-y-2 rounded-lg border border-tg-borderSoft bg-tg-panelAlt px-3 py-2.5">
          <Checkbox
            checked={form.can_delete_messages}
            onChange={(value) => patch({ can_delete_messages: value })}
            label="Может удалять сообщения"
            disabled={submitting}
          />
          <Checkbox
            checked={form.can_export}
            onChange={(value) => patch({ can_export: value })}
            label="Может экспортировать"
            disabled={submitting}
          />
          {mode === "edit" ? (
            <Checkbox
              checked={form.is_active}
              onChange={(value) => patch({ is_active: value })}
              label="Учётная запись активна"
              disabled={submitting}
            />
          ) : null}
        </div>

        {mode === "edit" && operator ? (
          <div className="space-y-2 rounded-lg border border-tg-border bg-tg-elevated px-3 py-2.5">
            <p className="flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-wider text-tg-textFaint">
              <KeyRound size={11} />
              Сбросить пароль
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="relative flex-1">
                <TextInput
                  type={passwordVisibleReset ? "text" : "password"}
                  value={newPassword}
                  onChange={(event) => {
                    setNewPassword(event.target.value);
                    setPasswordNote(null);
                  }}
                  disabled={resetBusy || submitting}
                  autoComplete="new-password"
                  placeholder="Новый пароль (минимум 8 символов)"
                  className="pr-9"
                />
                <button
                  type="button"
                  onClick={() => setPasswordVisibleReset((previous) => !previous)}
                  aria-label={passwordVisibleReset ? "Скрыть пароль" : "Показать пароль"}
                  className="absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-text"
                >
                  {passwordVisibleReset ? <EyeOff size={13} /> : <Eye size={13} />}
                </button>
              </div>
              <PrimaryButton
                onClick={() => void onResetPassword()}
                disabled={submitting || newPassword === ""}
                loading={resetBusy}
              >
                Сбросить пароль
              </PrimaryButton>
            </div>
            {passwordNote ? (
              <p
                className={`text-2xs ${
                  passwordNote === "Пароль обновлён" ? "text-tg-success" : "text-tg-danger"
                }`}
              >
                {passwordNote}
              </p>
            ) : (
              <p className="text-2xs text-tg-textFaint">Пароль применяется сразу, старый перестаёт работать.</p>
            )}
          </div>
        ) : null}

        {serverError ? (
          <p className="flex items-start gap-1.5 rounded-lg border border-tg-danger/40 bg-tg-danger/10 px-2.5 py-1.5 text-2xs text-tg-danger">
            <AlertCircle size={12} className="mt-[1px] shrink-0" />
            <span>{serverError}</span>
          </p>
        ) : null}
      </form>
    </Modal>
  );
}

function OperatorsTab({
  operators,
  loading,
  error,
  submitting,
  serverError,
  readOnly,
  onReload,
  onToggleActive,
  onDeactivate,
  onEdit,
  onCreate,
}: {
  operators: AdminOperator[];
  loading: boolean;
  error: string | null;
  submitting: boolean;
  serverError: string | null;
  /** SUPERVISOR: список виден, все мутации погашены (сервер ответит 403). */
  readOnly: boolean;
  onReload: () => void;
  onToggleActive: (operator: AdminOperator, value: boolean) => void;
  onDeactivate: (operator: AdminOperator) => void;
  onEdit: (operator: AdminOperator) => void;
  onCreate: () => void;
}): JSX.Element {
  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-tg-border px-4 py-2.5">
        <h2 className="text-xs font-semibold text-tg-textMuted">
          Операторы <span className="text-tg-textFaint">({operators.length})</span>
        </h2>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={onReload}
            disabled={loading}
            title="Обновить список"
            className="grid h-7 w-7 place-items-center rounded-lg border border-tg-border bg-tg-elevated text-tg-textMuted transition hover:text-tg-text disabled:opacity-60"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : undefined} />
          </button>
          <PrimaryButton
            onClick={onCreate}
            disabled={readOnly}
            title={readOnly ? "Создание операторов доступно только администратору" : undefined}
          >
            <Plus size={13} />
            Создать оператора
          </PrimaryButton>
        </div>
      </div>

      {error ? <ErrorBanner message={error} onRetry={onReload} /> : null}
      {serverError ? <ErrorBanner message={serverError} /> : null}

      <div className="min-h-0 flex-1 overflow-auto">
        {loading && operators.length === 0 ? (
          <SkeletonRows rows={6} />
        ) : operators.length === 0 ? (
          <EmptyState
            icon={UserCog}
            title="Операторов пока нет"
            hint="Создайте первую учётную запись — она появится в деск-панели после входа."
          />
        ) : (
          <table className="w-full min-w-[860px] border-collapse text-xs">
            <thead className="sticky top-0 z-10 bg-tg-panel text-2xs uppercase tracking-wider text-tg-textFaint">
              <tr className="border-b border-tg-border">
                <th className="px-4 py-2 text-left font-semibold">Оператор</th>
                <th className="px-3 py-2 text-left font-semibold">Логин</th>
                <th className="px-3 py-2 text-left font-semibold">Роль</th>
                <th className="px-3 py-2 text-left font-semibold">Статус</th>
                <th className="px-3 py-2 text-left font-semibold">Активен</th>
                <th className="px-3 py-2 text-left font-semibold">Права</th>
                <th className="px-4 py-2 text-right font-semibold">Действия</th>
              </tr>
            </thead>
            <tbody>
              {operators.map((operator) => {
                const shift = shiftOf(operator.shift_status);
                const color = operatorAvatarColor(operator);
                return (
                  <tr
                    key={operator.id}
                    className="border-b border-tg-borderSoft transition hover:bg-tg-elevated/60"
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2.5">
                        <span
                          className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-[11px] font-semibold text-white"
                          style={{ backgroundColor: color }}
                          aria-hidden="true"
                        >
                          {initials(operator.display_name || operator.username)}
                        </span>
                        <div className="min-w-0">
                          <p className="truncate font-medium text-tg-text">{operator.display_name}</p>
                          <p className="truncate text-2xs text-tg-textFaint">
                            {operator.max_chats === null ? "Лимит чатов: без ограничения" : `Лимит чатов: ${operator.max_chats}`}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 font-mono text-2xs text-tg-textMuted">{operator.username}</td>
                    <td className="px-3 py-2.5">
                      <RoleBadge role={operator.role} />
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="inline-flex items-center gap-1.5">
                        <span className={`shift-dot ${shift.dot}`} />
                        <span className={`text-2xs ${shift.text}`}>{shift.label}</span>
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <Toggle
                          checked={operator.is_active}
                          disabled={submitting}
                          readOnly={readOnly}
                          label={operator.is_active ? "Деактивировать" : "Активировать"}
                          onChange={(value) => onToggleActive(operator, value)}
                        />
                        <span className={`text-2xs ${operator.is_active ? "text-tg-textMuted" : "text-tg-danger"}`}>
                          {operator.is_active ? "Да" : "Нет"}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex flex-wrap gap-1">
                        {operator.can_delete_messages ? (
                          <span className="rounded border border-tg-border bg-tg-elevated px-1.5 py-0.5 text-[10px] text-tg-textMuted">
                            удаление
                          </span>
                        ) : null}
                        {operator.can_export ? (
                          <span className="rounded border border-tg-border bg-tg-elevated px-1.5 py-0.5 text-[10px] text-tg-textMuted">
                            экспорт
                          </span>
                        ) : null}
                        {!operator.can_delete_messages && !operator.can_export ? (
                          <span className="text-2xs text-tg-textFaint">—</span>
                        ) : null}
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          onClick={() => onEdit(operator)}
                          disabled={readOnly}
                          title={readOnly ? "Изменение операторов доступно только администратору" : undefined}
                          className="inline-flex items-center gap-1 rounded-md border border-tg-border bg-tg-elevated px-2 py-1 text-2xs text-tg-textMuted transition hover:border-tg-accent/50 hover:text-tg-text disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <Pencil size={11} />
                          Изменить
                        </button>
                        <button
                          type="button"
                          onClick={() => onDeactivate(operator)}
                          disabled={submitting || !operator.is_active || readOnly}
                          title={
                            readOnly
                              ? "Деактивация доступна только администратору"
                              : operator.is_active
                                ? "Деактивировать"
                                : "Учётная запись уже деактивирована"
                          }
                          className="inline-flex items-center gap-1 rounded-md border border-tg-danger/40 bg-tg-danger/10 px-2 py-1 text-2xs text-tg-danger transition hover:bg-tg-danger/20 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <Ban size={11} />
                          Деактивировать
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {loading && operators.length > 0 ? (
        <div className="flex items-center gap-1.5 border-t border-tg-border px-4 py-1.5 text-2xs text-tg-textFaint">
          <Loader2 size={11} className="animate-spin" />
          Обновление списка…
        </div>
      ) : null}
    </section>
  );
}

/* ============================================================= accounts */

interface AccountFormState {
  label: string;
  tg_username: string;
  phone: string;
  api_id: string;
  api_hash: string;
  session_string: string;
  is_active: boolean;
}

function emptyAccountForm(): AccountFormState {
  return {
    label: "",
    tg_username: "",
    phone: "",
    api_id: "",
    api_hash: "",
    session_string: "",
    is_active: true,
  };
}

function accountToForm(account: AdminAccount): AccountFormState {
  return {
    label: account.label,
    tg_username: account.tg_username ?? "",
    phone: account.phone ?? "",
    api_id: account.api_id === null ? "" : String(account.api_id),
    api_hash: "",
    session_string: "",
    is_active: account.is_active,
  };
}

function AccountModal({
  mode,
  account,
  submitting,
  serverError,
  onClose,
  onSaved,
}: {
  mode: "create" | "edit";
  account: AdminAccount | null;
  submitting: boolean;
  serverError: string | null;
  onClose: () => void;
  onSaved: () => void;
}): JSX.Element {
  const createAccount = useAdminStore((state) => state.createAccount);
  const updateAccount = useAdminStore((state) => state.updateAccount);
  const clearError = useAdminStore((state) => state.clearError);

  const [form, setForm] = useState<AccountFormState>(() =>
    mode === "edit" && account ? accountToForm(account) : emptyAccountForm(),
  );
  const [errors, setErrors] = useState<FieldErrors>({});
  const [sessionVisible, setSessionVisible] = useState(false);

  const labelRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (mode === "create") labelRef.current?.focus();
  }, [mode]);

  const patch = useCallback(
    (changes: Partial<AccountFormState>) => {
      setForm((previous) => ({ ...previous, ...changes }));
      if (serverError) clearError();
    },
    [clearError, serverError],
  );

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (submitting) return;

    const apiIdRaw = parseOptionalInt(form.api_id);
    const apiId = Number.isNaN(apiIdRaw) ? -1 : apiIdRaw;

    if (mode === "create") {
      const input: AccountCreateInput = {
        label: form.label.trim(),
        tg_username: parseOptionalText(form.tg_username)?.replace(/^@/, "") ?? null,
        phone: parseOptionalText(form.phone),
        api_id: apiId,
        api_hash: parseOptionalText(form.api_hash),
        session_string: parseOptionalText(form.session_string),
        is_active: form.is_active,
      };
      const nextErrors = validateAccountCreate(input);
      if (hasFieldErrors(nextErrors)) {
        setErrors(nextErrors);
        return;
      }
      setErrors({});
      const created = await createAccount({ ...input, api_id: apiIdRaw });
      if (created) onSaved();
      return;
    }

    if (!account) return;

    const input: AccountUpdateInput = {
      label: form.label.trim(),
      tg_username: parseOptionalText(form.tg_username)?.replace(/^@/, "") ?? null,
      phone: parseOptionalText(form.phone),
      api_id: apiId,
      api_hash: parseOptionalText(form.api_hash),
      session_string: parseOptionalText(form.session_string),
      is_active: form.is_active,
    };
    const nextErrors = validateAccountUpdate(input);
    if (hasFieldErrors(nextErrors)) {
      setErrors(nextErrors);
      return;
    }
    setErrors({});
    const updated = await updateAccount(account.id, { ...input, api_id: apiIdRaw });
    if (updated) onSaved();
  };

  return (
    <Modal
      title={mode === "create" ? "Новый Telegram-аккаунт" : `Аккаунт «${account?.label ?? ""}»`}
      subtitle="Секреты шифруются на сервере и никогда не возвращаются в интерфейс"
      wide
      onClose={onClose}
      footer={
        <>
          <GhostButton onClick={onClose} disabled={submitting}>
            Отмена
          </GhostButton>
          <button
            type="submit"
            form="account-form"
            disabled={submitting}
            className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-tg-accent px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-tg-accentHover disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? <Loader2 size={13} className="animate-spin" /> : null}
            {mode === "create" ? "Добавить" : "Сохранить"}
          </button>
        </>
      }
    >
      <form id="account-form" onSubmit={(event) => void onSubmit(event)} noValidate className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Метка" error={errors.label} hint="Например: Основной аккаунт">
            <TextInput
              ref={labelRef}
              value={form.label}
              onChange={(event) => patch({ label: event.target.value })}
              disabled={submitting}
              maxLength={64}
              autoComplete="off"
              placeholder="Основной аккаунт"
            />
          </Field>

          <Field label="@username" error={errors.tg_username}>
            <TextInput
              value={form.tg_username}
              onChange={(event) => patch({ tg_username: event.target.value })}
              disabled={submitting}
              maxLength={64}
              autoComplete="off"
              spellCheck={false}
              placeholder="tg_support"
            />
          </Field>

          <Field label="Телефон" error={errors.phone}>
            <TextInput
              value={form.phone}
              onChange={(event) => patch({ phone: event.target.value })}
              disabled={submitting}
              maxLength={32}
              autoComplete="off"
              placeholder="+7 900 000-00-00"
            />
          </Field>

          <Field label="api_id" error={errors.api_id}>
            <TextInput
              value={form.api_id}
              onChange={(event) => patch({ api_id: event.target.value.replace(/[^\d]/g, "") })}
              disabled={submitting}
              inputMode="numeric"
              autoComplete="off"
              placeholder="1234567"
            />
          </Field>
        </div>

        <Field
          label="api_hash"
          error={errors.api_hash}
          hint={
            mode === "edit"
              ? "Оставьте пустым, чтобы сохранить текущий api_hash"
              : "Выдаётся в my.telegram.org"
          }
        >
          <TextInput
            type="password"
            value={form.api_hash}
            onChange={(event) => patch({ api_hash: event.target.value })}
            disabled={submitting}
            maxLength={64}
            autoComplete="new-password"
            placeholder={mode === "edit" ? "•••••••• (не изменено)" : "0123456789abcdef0123456789abcdef"}
          />
        </Field>

        <Field
          label="session_string"
          error={errors.session_string}
          hint={
            mode === "edit"
              ? "Оставьте пустым, чтобы сохранить текущую сессию"
              : "Строка авторизации Telethon; хранится в зашифрованном виде"
          }
        >
          <div className="relative">
            <TextArea
              rows={4}
              value={form.session_string}
              onChange={(event) => patch({ session_string: event.target.value })}
              disabled={submitting}
              spellCheck={false}
              autoComplete="off"
              placeholder={mode === "edit" ? "Не изменено" : "1BVtsOK4Bu…"}
              className={`text-2xs ${sessionVisible ? "" : "[-webkit-text-security:disc]"}`}
            />
            <button
              type="button"
              onClick={() => setSessionVisible((previous) => !previous)}
              aria-label={sessionVisible ? "Скрыть сессию" : "Показать сессию"}
              className="absolute right-1.5 top-1.5 grid h-6 w-6 place-items-center rounded text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-text"
            >
              {sessionVisible ? <EyeOff size={13} /> : <Eye size={13} />}
            </button>
          </div>
        </Field>

        <div className="rounded-lg border border-tg-borderSoft bg-tg-panelAlt px-3 py-2.5">
          <Checkbox
            checked={form.is_active}
            onChange={(value) => patch({ is_active: value })}
            label="Аккаунт активен (используется воркером)"
            disabled={submitting}
          />
        </div>

        {serverError ? (
          <p className="flex items-start gap-1.5 rounded-lg border border-tg-danger/40 bg-tg-danger/10 px-2.5 py-1.5 text-2xs text-tg-danger">
            <AlertCircle size={12} className="mt-[1px] shrink-0" />
            <span>{serverError}</span>
          </p>
        ) : null}
      </form>
    </Modal>
  );
}

function AccountCard({
  account,
  submitting,
  readOnly,
  onEdit,
  onDelete,
}: {
  account: AdminAccount;
  submitting: boolean;
  /** SUPERVISOR: карточка только для чтения. */
  readOnly: boolean;
  onEdit: () => void;
  onDelete: () => void;
}): JSX.Element {
  return (
    <article className="flex flex-col gap-3 rounded-xl border border-tg-border bg-tg-panel p-4 transition hover:border-tg-hover">
      <div className="flex items-start gap-3">
        <span
          className="grid h-9 w-9 shrink-0 place-items-center rounded-full text-[11px] font-semibold text-white"
          style={{ backgroundColor: seedColor(account.label) }}
          aria-hidden="true"
        >
          {initials(account.label)}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-tg-text">{account.label}</p>
          <p className="truncate text-2xs text-tg-textMuted">
            {account.tg_username ? `@${account.tg_username.replace(/^@/, "")}` : "username не указан"}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span
            className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${
              account.has_session
                ? "border-tg-success/40 bg-tg-success/10 text-tg-success"
                : "border-tg-border bg-tg-elevated text-tg-textFaint"
            }`}
          >
            <CheckCircle2 size={10} />
            {account.has_session ? "Сессия есть" : "Нет сессии"}
          </span>
          {!account.is_active ? (
            <span className="rounded-md border border-tg-warning/40 bg-tg-warning/10 px-1.5 py-0.5 text-[10px] font-semibold text-tg-warning">
              выключен
            </span>
          ) : null}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-2xs">
        <div className="min-w-0">
          <dt className="text-tg-textFaint">Телефон</dt>
          <dd className="truncate font-mono text-tg-textMuted">{account.phone ?? "—"}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-tg-textFaint">api_id</dt>
          <dd className="truncate font-mono text-tg-textMuted">{account.api_id ?? "—"}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-tg-textFaint">tg_user_id</dt>
          <dd className="truncate font-mono text-tg-textMuted">{account.tg_user_id ?? "—"}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-tg-textFaint">Секреты</dt>
          <dd className="truncate text-tg-textFaint">скрыты</dd>
        </div>
      </dl>

      <div className="flex items-center justify-end gap-1.5 border-t border-tg-borderSoft pt-2">
        <button
          type="button"
          onClick={onEdit}
          disabled={readOnly}
          title={readOnly ? "Изменение аккаунтов доступно только администратору" : undefined}
          className="inline-flex items-center gap-1 rounded-md border border-tg-border bg-tg-elevated px-2 py-1 text-2xs text-tg-textMuted transition hover:border-tg-accent/50 hover:text-tg-text disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Pencil size={11} />
          Изменить
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={submitting || readOnly}
          title={readOnly ? "Удаление аккаунтов доступно только администратору" : undefined}
          className="inline-flex items-center gap-1 rounded-md border border-tg-danger/40 bg-tg-danger/10 px-2 py-1 text-2xs text-tg-danger transition hover:bg-tg-danger/20 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Trash2 size={11} />
          Удалить
        </button>
      </div>
    </article>
  );
}

function AccountsTab({
  accounts,
  loading,
  error,
  submitting,
  serverError,
  readOnly,
  onReload,
  onCreate,
  onEdit,
  onDelete,
}: {
  accounts: AdminAccount[];
  loading: boolean;
  error: string | null;
  submitting: boolean;
  serverError: string | null;
  /** SUPERVISOR: список виден, все мутации погашены (сервер ответит 403). */
  readOnly: boolean;
  onReload: () => void;
  onCreate: () => void;
  onEdit: (account: AdminAccount) => void;
  onDelete: (account: AdminAccount) => void;
}): JSX.Element {
  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-tg-border px-4 py-2.5">
        <h2 className="text-xs font-semibold text-tg-textMuted">
          Telegram-аккаунты <span className="text-tg-textFaint">({accounts.length})</span>
        </h2>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={onReload}
            disabled={loading}
            title="Обновить список"
            className="grid h-7 w-7 place-items-center rounded-lg border border-tg-border bg-tg-elevated text-tg-textMuted transition hover:text-tg-text disabled:opacity-60"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : undefined} />
          </button>
          <PrimaryButton
            onClick={onCreate}
            disabled={readOnly}
            title={readOnly ? "Добавление аккаунтов доступно только администратору" : undefined}
          >
            <Plus size={13} />
            Добавить аккаунт
          </PrimaryButton>
        </div>
      </div>

      {error ? <ErrorBanner message={error} onRetry={onReload} /> : null}
      {serverError ? <ErrorBanner message={serverError} /> : null}

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {loading && accounts.length === 0 ? (
          <SkeletonRows rows={4} />
        ) : accounts.length === 0 ? (
          <EmptyState
            icon={Database}
            title="Аккаунты не подключены"
            hint="Добавьте api_id, api_hash и строку сессии Telethon, чтобы воркер мог читать и отправлять сообщения."
          />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {accounts.map((account) => (
              <AccountCard
                key={account.id}
                account={account}
                submitting={submitting}
                readOnly={readOnly}
                onEdit={() => onEdit(account)}
                onDelete={() => onDelete(account)}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

/* ================================================================ audit */

function AuditRow({ entry }: { entry: AuditEntry }): JSX.Element {
  return (
    <li className="flex flex-col gap-1 border-b border-tg-borderSoft px-4 py-2.5 transition hover:bg-tg-elevated/50 sm:flex-row sm:items-start sm:gap-3">
      <div className="flex shrink-0 items-center gap-2 font-mono text-2xs tabular text-tg-textFaint sm:w-[9.5rem]">
        <ScrollText size={11} />
        {formatAuditTime(entry.created_at)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-xs font-medium text-tg-text">{actionLabel(entry.action)}</span>
          <span className="rounded border border-tg-border bg-tg-elevated px-1.5 py-0.5 font-mono text-[10px] text-tg-textFaint">
            {entry.action}
          </span>
          <span className="text-2xs text-tg-textMuted">{targetLabel(entry)}</span>
        </div>
        {entry.detail ? <p className="mt-0.5 break-words text-2xs text-tg-textMuted">{entry.detail}</p> : null}
      </div>
      <div className="shrink-0 text-2xs text-tg-textMuted sm:w-[8rem] sm:text-right">
        {entry.actor_username ? (
          <span className="font-mono">{entry.actor_username}</span>
        ) : (
          <span className="text-tg-textFaint">система</span>
        )}
      </div>
    </li>
  );
}

function AuditTab({
  audit,
  loading,
  error,
  onReload,
}: {
  audit: AuditEntry[];
  loading: boolean;
  error: string | null;
  onReload: () => void;
}): JSX.Element {
  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-tg-border px-4 py-2.5">
        <h2 className="text-xs font-semibold text-tg-textMuted">
          Журнал действий <span className="text-tg-textFaint">({audit.length})</span>
        </h2>
        <div className="ml-auto">
          <button
            type="button"
            onClick={onReload}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-lg border border-tg-border bg-tg-elevated px-2.5 py-1.5 text-xs text-tg-textMuted transition hover:text-tg-text disabled:opacity-60"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : undefined} />
            Обновить
          </button>
        </div>
      </div>

      {error ? <ErrorBanner message={error} onRetry={onReload} /> : null}

      <div className="min-h-0 flex-1 overflow-auto">
        {loading && audit.length === 0 ? (
          <SkeletonRows rows={7} />
        ) : audit.length === 0 ? (
          <EmptyState
            icon={History}
            title="Пока нет записей"
            hint="Здесь появятся все действия администраторов: создание операторов, правки аккаунтов, входы в панель."
          />
        ) : (
          <ul>
            {audit.map((entry) => (
              <AuditRow key={entry.id} entry={entry} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/* ================================================================== page */

type OperatorModalState = { mode: "create" } | { mode: "edit"; operator: AdminOperator } | null;
type AccountModalState = { mode: "create" } | { mode: "edit"; account: AdminAccount } | null;

export default function AdminPage(): JSX.Element {
  /* --- стабильные селекторы: только ссылки на поля стора ---------------- */
  const token = useAdminStore((state) => state.token);
  const operator = useAdminStore((state) => state.operator);

  const operators = useAdminStore((state) => state.operators);
  const operatorsLoading = useAdminStore((state) => state.operatorsLoading);
  const operatorsError = useAdminStore((state) => state.operatorsError);

  const accounts = useAdminStore((state) => state.accounts);
  const accountsLoading = useAdminStore((state) => state.accountsLoading);
  const accountsError = useAdminStore((state) => state.accountsError);

  const audit = useAdminStore((state) => state.audit);
  const auditLoading = useAdminStore((state) => state.auditLoading);
  const auditError = useAdminStore((state) => state.auditError);

  const settings = useAdminStore((state) => state.settings);
  const settingsLoading = useAdminStore((state) => state.settingsLoading);
  const settingsError = useAdminStore((state) => state.settingsError);
  const settingsSaving = useAdminStore((state) => state.settingsSaving);
  const settingsToast = useAdminStore((state) => state.settingsToast);

  const submitting = useAdminStore((state) => state.submitting);
  const mutationError = useAdminStore((state) => state.mutationError);

  const loadOperators = useAdminStore((state) => state.loadOperators);
  const loadAccounts = useAdminStore((state) => state.loadAccounts);
  const loadAudit = useAdminStore((state) => state.loadAudit);
  const loadSettings = useAdminStore((state) => state.loadSettings);
  const saveSettings = useAdminStore((state) => state.saveSettings);
  const updateOperator = useAdminStore((state) => state.updateOperator);
  const deactivateOperator = useAdminStore((state) => state.deactivateOperator);
  const resetPassword = useAdminStore((state) => state.resetPassword);
  const deleteAccount = useAdminStore((state) => state.deleteAccount);
  const logout = useAdminStore((state) => state.logout);
  const clearError = useAdminStore((state) => state.clearError);

  const [tab, setTab] = useState<TabKey>("operators");
  const [operatorModal, setOperatorModal] = useState<OperatorModalState>(null);
  const [accountModal, setAccountModal] = useState<AccountModalState>(null);

  const isSupervisor = operator ? operator.role.toUpperCase() !== "ADMIN" : false;
  const active = operator;

  /* --- авторизация ---------------------------------------------------- */
  useEffect(() => {
    if (!token || !active) window.location.href = LOGIN_PATH;
  }, [token, active]);

  /* --- данные активной вкладки ---------------------------------------- */
  useEffect(() => {
    if (!token || !active) return;
    if (tab === "operators") void loadOperators();
    else if (tab === "accounts") void loadAccounts();
    else if (tab === "settings") void loadSettings();
    else void loadAudit();
  }, [tab, token, active, loadOperators, loadAccounts, loadSettings, loadAudit]);

  /* --- счётчики в шапке (мемоизация, не в селекторе) ------------------ */
  const activeCount = useMemo(() => operators.filter((item) => item.is_active).length, [operators]);
  const sessionCount = useMemo(() => accounts.filter((item) => item.has_session).length, [accounts]);

  const handleSaveSettings = useCallback(
    (patch: AdminSettingsPatch) => {
      if (isSupervisor) return;
      void saveSettings(patch);
    },
    [isSupervisor, saveSettings],
  );

  /* --- обработчики ---------------------------------------------------- */
  const handleReload = useCallback(() => {
    if (tab === "operators") void loadOperators(true);
    else if (tab === "accounts") void loadAccounts(true);
    else if (tab === "settings") void loadSettings(true);
    else void loadAudit(true);
  }, [tab, loadOperators, loadAccounts, loadSettings, loadAudit]);

  const handleLogout = useCallback(() => {
    void logout().then(() => {
      window.location.href = LOGIN_PATH;
    });
  }, [logout]);

  const handleBackToDesk = useCallback(() => {
    window.location.href = DESK_PATH;
  }, []);

  const handleToggleActive = useCallback(
    (target: AdminOperator, value: boolean) => {
      /* SUPERVISOR: сервер ответит 403 — не отправляем запрос вовсе. */
      if (isSupervisor) return;
      if (!value && !window.confirm(`Деактивировать оператора ${target.username}? Вход в панель будет заблокирован.`)) {
        return;
      }
      void updateOperator(target.id, {
        display_name: target.display_name,
        role: roleOf(target.role),
        max_chats: target.max_chats,
        can_delete_messages: target.can_delete_messages,
        can_export: target.can_export,
        is_active: value,
        avatar_color: target.avatar_color,
      });
    },
    [isSupervisor, updateOperator],
  );

  const handleDeactivate = useCallback(
    (target: AdminOperator) => {
      if (isSupervisor) return;
      if (!window.confirm(`Деактивировать оператора ${target.username}? Учётная запись сохранится, но вход будет закрыт.`)) {
        return;
      }
      void deactivateOperator(target.id);
    },
    [isSupervisor, deactivateOperator],
  );

  const handleDeleteAccount = useCallback(
    (target: AdminAccount) => {
      if (isSupervisor) return;
      if (!window.confirm(`Удалить Telegram-аккаунт «${target.label}»? Действие необратимо.`)) return;
      void deleteAccount(target.id);
    },
    [isSupervisor, deleteAccount],
  );

  const handlePasswordReset = useCallback(
    async (id: number, password: string): Promise<boolean> =>
      isSupervisor ? false : resetPassword(id, password),
    [isSupervisor, resetPassword],
  );

  const closeOperatorModal = useCallback(() => {
    setOperatorModal(null);
    clearError();
  }, [clearError]);

  const closeAccountModal = useCallback(() => {
    setAccountModal(null);
    clearError();
  }, [clearError]);

  const savedOperator = useCallback(() => {
    setOperatorModal(null);
    clearError();
  }, [clearError]);

  const savedAccount = useCallback(() => {
    setAccountModal(null);
    clearError();
  }, [clearError]);

  /* Пока сессия не подтверждена — не мигаем содержимым панели. */
  if (!token || !active) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-tg-bg">
        <div className="flex items-center gap-2 text-xs text-tg-textFaint">
          <Loader2 size={14} className="animate-spin" />
          Перенаправление на вход…
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-tg-bg text-tg-text">
      <header className="z-20 flex h-14 shrink-0 items-center gap-3 border-b border-tg-border bg-tg-panel px-3 sm:px-4">
        <div className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-tg-accent/20 text-tg-accentHover">
          <ShieldCheck size={17} />
        </div>
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold tracking-tight">Администрирование TG-Desk</h1>
          <p className="hidden truncate text-2xs text-tg-textFaint sm:block">
            Операторов: {operators.length === 0 ? "—" : `${activeCount} из ${operators.length}`} · аккаунтов с
            сессией: {accounts.length === 0 ? "—" : `${sessionCount} из ${accounts.length}`}
          </p>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={handleBackToDesk}
            className="hidden items-center gap-1 rounded-lg border border-tg-border bg-tg-elevated px-2.5 py-1.5 text-xs text-tg-textMuted transition hover:text-tg-text sm:inline-flex"
          >
            <ChevronLeft size={13} />
            К работе
          </button>

          <div className="flex items-center gap-2 rounded-lg border border-tg-border bg-tg-panelAlt px-2 py-1">
            <span
              className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-[10px] font-semibold text-white"
              style={{ backgroundColor: active.avatar_color ?? seedColor(active.username) }}
              aria-hidden="true"
            >
              {initials(active.display_name || active.username)}
            </span>
            <div className="hidden leading-tight sm:block">
              <p className="max-w-[11rem] truncate text-2xs font-medium text-tg-text">{active.display_name}</p>
              <p className="text-[10px] uppercase tracking-wider text-tg-textFaint">{active.role}</p>
            </div>
          </div>

          <button
            type="button"
            onClick={handleLogout}
            className="inline-flex items-center gap-1 rounded-lg border border-tg-danger/40 bg-tg-danger/10 px-2.5 py-1.5 text-xs font-medium text-tg-danger transition hover:bg-tg-danger/20"
          >
            <LogOut size={13} />
            Выйти
          </button>
        </div>
      </header>

      <nav className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-tg-border bg-tg-panel px-2 sm:px-4">
        {TABS.map((item) => {
          const Icon = item.icon;
          const isActive = item.key === tab;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setTab(item.key)}
              aria-current={isActive ? "page" : undefined}
              className={`-mb-px inline-flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2.5 text-xs font-medium transition ${
                isActive
                  ? "border-tg-accent text-tg-accentHover"
                  : "border-transparent text-tg-textMuted hover:text-tg-text"
              }`}
            >
              <Icon size={13} />
              {item.label}
            </button>
          );
        })}
      </nav>

      {isSupervisor ? (
        <div className="flex shrink-0 items-center gap-2 border-b border-tg-warning/30 bg-tg-warning/10 px-4 py-1.5 text-2xs text-tg-warning">
          <AlertCircle size={12} className="shrink-0" />
          Режим супервизора: доступен только просмотр списка операторов. Изменения выполняет администратор.
        </div>
      ) : null}

      <main className="flex min-h-0 flex-1 flex-col">
        {tab === "operators" ? (
          <OperatorsTab
            operators={operators}
            loading={operatorsLoading}
            error={operatorsError}
            submitting={submitting}
            serverError={mutationError}
            readOnly={isSupervisor}
            onReload={handleReload}
            onToggleActive={handleToggleActive}
            onDeactivate={handleDeactivate}
            onEdit={(target) => setOperatorModal({ mode: "edit", operator: target })}
            onCreate={() => setOperatorModal({ mode: "create" })}
          />
        ) : null}

        {tab === "accounts" ? (
          <AccountsTab
            accounts={accounts}
            loading={accountsLoading}
            error={accountsError}
            submitting={submitting}
            serverError={mutationError}
            readOnly={isSupervisor}
            onReload={handleReload}
            onCreate={() => setAccountModal({ mode: "create" })}
            onEdit={(target) => setAccountModal({ mode: "edit", account: target })}
            onDelete={handleDeleteAccount}
          />
        ) : null}

        {tab === "settings" ? (
          <SettingsTab
            settings={settings}
            loading={settingsLoading}
            error={settingsError}
            saving={settingsSaving}
            toast={settingsToast}
            readOnly={isSupervisor}
            onReload={handleReload}
            onSave={handleSaveSettings}
          />
        ) : null}

        {tab === "audit" ? (
          <AuditTab audit={audit} loading={auditLoading} error={auditError} onReload={handleReload} />
        ) : null}
      </main>

      {operatorModal ? (
        <OperatorModal
          mode={operatorModal.mode}
          operator={operatorModal.mode === "edit" ? operatorModal.operator : null}
          submitting={submitting}
          serverError={mutationError}
          onClose={closeOperatorModal}
          onSaved={savedOperator}
          onPasswordReset={handlePasswordReset}
        />
      ) : null}

      {accountModal ? (
        <AccountModal
          mode={accountModal.mode}
          account={accountModal.mode === "edit" ? accountModal.account : null}
          submitting={submitting}
          serverError={mutationError}
          onClose={closeAccountModal}
          onSaved={savedAccount}
        />
      ) : null}
    </div>
  );
}
