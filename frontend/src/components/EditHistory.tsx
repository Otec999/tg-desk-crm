import { ChevronDown, ChevronUp, History, TriangleAlert } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { formatClock, parseDate } from "../lib/format";

/**
 * CONTRACTS_V3 §2.2 — одна запись таблицы `message_edits`
 * (REST-контракт `MessageEditOut`, §2.3).
 *
 * Объявлена локально, чтобы компонент компилировался независимо от владельца
 * `lib/types.ts`; структурно совместима с `MessageEdit`.
 */
export interface EditHistoryEntry {
  id: number;
  message_id: number;
  source: "TELEGRAM" | "CRM";
  old_text: string;
  new_text: string;
  operator_id: number | null;
  actor_name: string | null;
  created_at: string;
}

export interface EditHistoryProps {
  edits: EditHistoryEntry[];
  loading?: boolean;
  error?: string | null;
  /** Раскрыт ли блок. Если не передан — компонент переключает состояние сам. */
  open?: boolean;
  onToggle?: () => void;
}

/** Единственный источник истории правок (CONTRACTS_V3 §2.1, инвариант §5.3). */
const TELEGRAM_SOURCE = "TELEGRAM";

/** Порог, после которого текст сворачивается и появляется «Показать полностью». */
const TRUNCATE_THRESHOLD = 200;

/** Сколько символов оставляем видимыми в свёрнутом виде. */
const TRUNCATE_KEEP = 160;

const TOGGLE_CLASS =
  "inline-flex items-center gap-1 text-2xs text-tg-textMuted hover:text-tg-text transition-colors";

const WARNING_TEXT = "Текст изменён клиентом через Telegram";

/**
 * CONTRACTS_V3 §2.1 / §3.1 — раскрывающийся блок истории правок под сообщением.
 *
 * Ключевое бизнес-требование: история ведётся **только** для правок из Telegram
 * (`source === "TELEGRAM"`) — клиент мог подменить текст, и мы обязаны видеть,
 * что было написано изначально. Правки оператора из CRM (`source === "CRM"`)
 * истории не имеют и в списке не отображаются никогда.
 *
 * Компонент не делает запросов: данные приходят из `store.editHistory[messageId]`.
 */
export function EditHistory({
  edits,
  loading = false,
  error = null,
  open,
  onToggle,
}: EditHistoryProps): JSX.Element | null {
  /* Автономный режим, когда родитель не контролирует раскрытие. */
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const isOpen = open ?? uncontrolledOpen;

  /* Длинные тексты раскрываются независимо для каждой записи — по её `id`. */
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<number>>(() => new Set<number>());

  /* Только Telegram-правки, новые сверху. Невалидные даты уходят в конец.
     Не-массив из API (регрессия ответа) не должен ронять весь чат. */
  const telegramEdits = useMemo<EditHistoryEntry[]>(() => {
    const source = Array.isArray(edits) ? edits : [];
    const filtered = source.filter(
      (entry): entry is EditHistoryEntry =>
        typeof entry === "object" && entry !== null && entry.source === TELEGRAM_SOURCE,
    );
    return filtered.slice().sort((left, right) => {
      const delta = editRank(right.created_at) - editRank(left.created_at);
      if (delta !== 0 && Number.isFinite(delta)) return delta;
      return right.id - left.id;
    });
  }, [edits]);

  const handleToggle = useCallback((): void => {
    if (onToggle) {
      onToggle();
      return;
    }
    setUncontrolledOpen((previous) => !previous);
  }, [onToggle]);

  const toggleEntry = useCallback((entryId: number): void => {
    setExpandedIds((previous) => {
      const next = new Set(previous);
      if (next.has(entryId)) next.delete(entryId);
      else next.add(entryId);
      return next;
    });
  }, []);

  const hasError = typeof error === "string" && error.trim().length > 0;
  const count = telegramEdits.length;

  /* Ни правок, ни загрузки, ни ошибки → блока не существует. */
  if (count === 0 && !loading && !hasError) return null;

  const label = loading ? "Загрузка…" : hasError ? "Ошибка" : `Изменено из Telegram · ${count}`;

  const Chevron = isOpen ? ChevronUp : ChevronDown;

  return (
    <div className="mt-1 flex min-w-0 max-w-full flex-col">
      <button
        type="button"
        onClick={handleToggle}
        aria-expanded={isOpen}
        title={hasError && error ? error : label}
        className={TOGGLE_CLASS}
      >
        <History size={11} className="shrink-0" aria-hidden />
        <span className="truncate">{label}</span>
        <Chevron size={11} className="shrink-0" aria-hidden />
      </button>

      {isOpen && (
        <div
          role="region"
          aria-label="История изменений"
          className="mt-1 space-y-2 rounded-lg border border-tg-border bg-tg-elevated/60 p-2"
        >
          {loading && <HistorySkeleton />}

          {!loading && hasError && (
            <p role="alert" className="flex items-start gap-1 text-2xs text-tg-danger">
              <TriangleAlert size={10} className="mt-[2px] shrink-0" aria-hidden />
              <span className="break-words">{error}</span>
            </p>
          )}

          {telegramEdits.map((entry, index) => (
            <EditRecord
              key={entry.id}
              entry={entry}
              /* Сортировка desc: индекс 0 — самая новая правка, она же «первая» в списке. */
              first={index === 0}
              expanded={expandedIds.has(entry.id)}
              onToggleEntry={() => toggleEntry(entry.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- helpers */

/** Время сортировки; битые даты дают `-Infinity` и не ломают компаратор. */
function editRank(value: string | null | undefined): number {
  return parseDate(value)?.getTime() ?? Number.NEGATIVE_INFINITY;
}

/**
 * `created_at` → `14:05 · 12.03.2025` (либо `· Сегодня` / `· Вчера`).
 * Невалидная дата даёт пустую строку — компонент не падает.
 */
function formatEditMoment(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "";
  const clock = formatClock(value);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return clock ? `${clock} · Сегодня` : "Сегодня";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return clock ? `${clock} · Вчера` : "Вчера";
  const day = date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  return clock ? `${clock} · ${day}` : day;
}

function HistorySkeleton(): JSX.Element {
  return (
    <div className="space-y-2" role="status" aria-label="Загрузка истории изменений">
      <div className="h-3 w-full animate-pulse rounded bg-tg-border/40" />
      <div className="h-3 w-3/4 animate-pulse rounded bg-tg-border/40" />
    </div>
  );
}

function EditRecord({
  entry,
  first,
  expanded,
  onToggleEntry,
}: {
  entry: EditHistoryEntry;
  /** Первая (самая новая) запись несёт предупреждение о подмене текста. */
  first: boolean;
  expanded: boolean;
  onToggleEntry: () => void;
}): JSX.Element {
  const author = entry.actor_name?.trim() ? entry.actor_name : "Клиент";
  const moment = formatEditMoment(entry.created_at);

  return (
    <div className="min-w-0 space-y-1 rounded-md border border-tg-borderSoft bg-tg-panel/40 p-1.5">
      <div className="flex items-center gap-1 text-2xs text-tg-textMuted">
        <span className="truncate font-medium text-tg-text">{author}</span>
        {moment && (
          <>
            <span aria-hidden>·</span>
            <span className="tabular shrink-0">{moment}</span>
          </>
        )}
      </div>

      {first && (
        <div className="flex items-start gap-1 rounded border border-tg-warning/30 bg-tg-warning/10 px-1.5 py-1 text-2xs font-semibold text-tg-warning">
          <TriangleAlert size={11} className="mt-[1px] shrink-0" aria-hidden />
          <span className="break-words">{WARNING_TEXT}</span>
        </div>
      )}

      <VersionBlock
        label="Было:"
        text={entry.old_text ?? ""}
        emptyLabel="(пусто)"
        tone="text-tg-textMuted"
        background="bg-tg-danger/10"
        strikeWhenShort
        expanded={expanded}
        onToggleEntry={onToggleEntry}
      />
      <VersionBlock
        label="Стало:"
        text={entry.new_text ?? ""}
        emptyLabel="(удалён весь текст)"
        tone="text-tg-text"
        background="bg-tg-warning/10"
        expanded={expanded}
        onToggleEntry={onToggleEntry}
      />
    </div>
  );
}

function VersionBlock({
  label,
  text,
  emptyLabel,
  tone,
  background,
  strikeWhenShort = false,
  expanded,
  onToggleEntry,
}: {
  label: string;
  text: string;
  emptyLabel: string;
  tone: string;
  background: string;
  /** Короткий старый текст показываем зачёркнутым — это «удалённая» версия. */
  strikeWhenShort?: boolean;
  expanded: boolean;
  onToggleEntry: () => void;
}): JSX.Element {
  const isEmpty = text.trim().length === 0;
  const isLong = text.length > TRUNCATE_THRESHOLD;
  const collapsed = isLong && !expanded;
  /* Свёрнутый вид ограничиваем тремя строками через `line-clamp-3` и дублируем
     запасным срезом по символам — так блок не раздувает пузырь в любом браузере. */
  const shown = isEmpty ? emptyLabel : collapsed ? `${text.slice(0, TRUNCATE_KEEP)}…` : text;
  const struck = strikeWhenShort && !isEmpty && !isLong;

  return (
    <div className="min-w-0">
      <div className="text-2xs text-tg-textMuted">{label}</div>
      <div
        title={isEmpty ? emptyLabel : text}
        className={`mt-0.5 whitespace-pre-wrap break-words rounded px-2 py-1 text-xs leading-[1.35] ${background} ${
          isEmpty ? "italic text-tg-textMuted" : tone
        } ${struck ? "text-tg-textMuted line-through decoration-tg-danger/60" : ""} ${
          collapsed ? "line-clamp-3" : ""
        }`}
      >
        {shown}
      </div>
      {isLong && (
        <button
          type="button"
          onClick={onToggleEntry}
          aria-expanded={expanded}
          className="mt-0.5 inline-flex items-center gap-0.5 text-2xs text-tg-accentHover transition-colors hover:text-tg-accent"
        >
          {expanded ? <ChevronUp size={10} aria-hidden /> : <ChevronDown size={10} aria-hidden />}
          {expanded ? "Свернуть" : "Показать полностью"}
        </button>
      )}
    </div>
  );
}

export default EditHistory;
