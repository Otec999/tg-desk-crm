/**
 * ReactionBar — чипы реакций под сообщением + пикер из шести эмодзи.
 *
 * Контракт: CONTRACTS_V2 §3.2 (`GET/PUT/DELETE /api/messages/{id}/reaction`) и §6.
 * Компонент управляемый: состояние живёт на сервере, наружу отдаются только
 * намерения оператора `onAdd(emoji)` / `onRemove()`.
 */
import { SmilePlus } from "lucide-react";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import type { ReactionSummary } from "../lib/types";

export interface ReactionBarProps {
  messageId: number;
  chatId: number;
  reactions: ReactionSummary[];
  onAdd: (emoji: string) => void;
  onRemove: () => void;
  isOwnMessage?: boolean;
  disabled?: boolean;
  align?: "left" | "right";
}

/* --------------------------------------------------------------- константы */

/**
 * Ровно шесть разрешённых эмодзи — зеркало `ReactionEmoji` (`app/enums.py`).
 * `❤️` записан как `\u2764\uFE0F`, чтобы редакторы и линтеры не теряли
 * вариативный селектор и строка гарантированно совпадала с бэкендом.
 */
export const ALLOWED_REACTIONS = ["👍", "\u2764\uFE0F", "🔥", "👀", "👏", "🙏"] as const;

/** Русские подписи эмодзи (для `aria-label`, `title` и подсказок пикера). */
export const REACTION_LABELS: Record<string, string> = {
  "👍": "Нравится",
  "\u2764\uFE0F": "Сердце",
  "🔥": "Огонь",
  "👀": "Смотрю",
  "👏": "Аплодисменты",
  "🙏": "Спасибо",
};

/** Те же подписи, но с ключом по базовой графеме («❤» без VS16). */
const LABEL_BY_KEY = new Map<string, string>();
/** Список разрешённых эмодзи, нормализованный для сравнения. */
const ALLOWED_KEYS: string[] = [];

const PICKER_EMOJIS: readonly string[] = ALLOWED_REACTIONS;

/** Слушатели вешаются в фазе перехвата, чтобы панель закрылась до обработчиков React. */
const DOCUMENT_EVENTS: ("pointerdown" | "click")[] = ["pointerdown", "click"];
/** События старше этого окна случились ДО открытия панели — их игнорируем. */
const OPEN_GUARD_MS = 120;
const PULSE_MS = 260;
const COUNT_MS = 420;

/* ------------------------------------------------------------- утилиты текста */

interface SegmentLike {
  segment: string;
}

type SegmenterCtor = new (locale?: string, options?: { granularity: "grapheme" }) => {
  segment(input: string): Iterable<SegmentLike>;
};

const GRAPHEME_SPLITTER: ((input: string) => string[]) | null = (() => {
  const Segmenter = (Intl as unknown as { Segmenter?: SegmenterCtor }).Segmenter;
  if (typeof Segmenter !== "function") return null;
  try {
    const segmenter = new Segmenter("ru", { granularity: "grapheme" });
    return (input: string): string[] => {
      const parts: string[] = [];
      for (const item of segmenter.segment(input)) parts.push(item.segment);
      return parts;
    };
  } catch {
    return null;
  }
})();

/** Разбиение на графемы с запасным путём для сред без `Intl.Segmenter`. */
function splitGraphemes(input: string): string[] {
  if (GRAPHEME_SPLITTER) return GRAPHEME_SPLITTER(input);
  return Array.from(input);
}

/** Первая графема строки или `""` для пустой строки. */
function baseGrapheme(input: string): string {
  return splitGraphemes(input)[0] ?? "";
}

/**
 * Ключ сравнения эмодзи без вариативных селекторов и модификаторов тона.
 *
 * `Intl.Segmenter` держит «❤️» (U+2764 + VS16) единой графемой, поэтому для
 * ключа берём только базовые символы без `U+FE0E/U+FE0F` и эмодзи-модификаторов
 * `U+1F3FB..U+1F3FF`. Так «❤» и «❤️», «👍» и «👍🏽» схлопываются в один чипс.
 */
const EMOJI_KEY_STRIP = /[\uFE0E\uFE0F\u{1F3FB}-\u{1F3FF}]/gu;

function emojiKey(emoji: string): string {
  const base = baseGrapheme(emoji.trim());
  return base.length === 0 ? "" : base.replace(EMOJI_KEY_STRIP, "");
}

// Populated after `emojiKey`/`EMOJI_KEY_STRIP` exist: building the lookup earlier
// hits the temporal dead zone and crashes the module at import time.
for (const emoji of ALLOWED_REACTIONS) {
  const key = emojiKey(emoji);
  LABEL_BY_KEY.set(key, REACTION_LABELS[emoji] ?? emoji);
  ALLOWED_KEYS.push(key);
}

/**
 * Приводит эмодзи к точной серверной форме из `ALLOWED_REACTIONS`.
 *
 * Нужен, когда данные пришли в укороченном виде («❤» без VS16): серверный
 * `PUT` принимает строгое значение из enum, поэтому в запрос уходит
 * «❤️». Некорректные эмодзи возвращаются как есть — их валидирует бэкенд.
 */
function toWireEmoji(emoji: string): string {
  const key = emojiKey(emoji);
  if (key.length === 0) return emoji;
  const index = ALLOWED_KEYS.indexOf(key);
  return index >= 0 ? ALLOWED_REACTIONS[index] : emoji;
}

/** Русская подпись эмодзи; для неизвестных данных — сам эмодзи. */
function reactionLabel(emoji: string): string {
  return LABEL_BY_KEY.get(emojiKey(emoji)) ?? emoji;
}

/** «Анна, Борис и Даниил» — через `Intl.ListFormat`, с ручным фолбэком. */
const NAME_LIST_FORMATTER: ((names: string[]) => string) | null = (() => {
  const ListFormat = (
    Intl as unknown as { ListFormat?: new (locale?: string, options?: object) => { format(items: string[]): string } }
  ).ListFormat;
  if (typeof ListFormat !== "function") return null;
  try {
    const formatter = new ListFormat("ru", { style: "long", type: "conjunction" });
    return (names: string[]): string => formatter.format(names);
  } catch {
    return null;
  }
})();

function formatNameList(names: readonly string[]): string {
  if (names.length === 0) return "";
  if (NAME_LIST_FORMATTER) return NAME_LIST_FORMATTER([...names]);
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(", ")} и ${names[names.length - 1]}`;
}

/* ------------------------------------------------------------- утилиты React */

/** Установка/снятие DOM-таймера без «утечек» при размонтировании. */
function useTimers(): {
  set: (key: string, callback: () => void, delay: number) => void;
  clear: (key: string) => void;
  clearAll: () => void;
} {
  const timers = useRef(new Map<string, number>());

  const clear = useCallback((key: string): void => {
    const handle = timers.current.get(key);
    if (handle !== undefined) {
      window.clearTimeout(handle);
      timers.current.delete(key);
    }
  }, []);

  const clearAll = useCallback((): void => {
    for (const handle of timers.current.values()) window.clearTimeout(handle);
    timers.current.clear();
  }, []);

  const set = useCallback((key: string, callback: () => void, delay: number): void => {
    const previous = timers.current.get(key);
    if (previous !== undefined) window.clearTimeout(previous);
    const handle = window.setTimeout(() => {
      timers.current.delete(key);
      callback();
    }, delay);
    timers.current.set(key, handle);
  }, []);

  return useMemo(() => ({ set, clear, clearAll }), [set, clear, clearAll]);
}

const hasMatchMedia = (): boolean =>
  typeof window !== "undefined" && typeof window.matchMedia === "function";

/** Устройство без hover (телефон/планшет): экшн-кнопка видна всегда. */
function detectTouchDevice(): boolean {
  if (!hasMatchMedia()) return false;
  if (window.matchMedia("(hover: none)").matches) return true;
  return window.matchMedia("(pointer: coarse)").matches;
}

/**
 * Пульс, который ничего не ломает: длительность привязана к JS-таймеру,
 * поэтому `prefers-reduced-motion` (обнуляющий CSS-анимации) не оставит чипс
 * замороженным в увеличенном состоянии.
 */
function usePulse(): {
  pulseEmoji: string | null;
  pulseCount: boolean;
  pulse: (emoji: string, forCount: boolean) => void;
  reset: () => void;
} {
  const timers = useTimers();
  const [pulseEmoji, setPulseEmoji] = useState<string | null>(null);
  const [pulseCount, setPulseCount] = useState(false);

  const reset = useCallback((): void => {
    timers.clear("pulse");
    timers.clear("count");
    setPulseEmoji(null);
    setPulseCount(false);
  }, [timers]);

  const pulse = useCallback(
    (emoji: string, forCount: boolean): void => {
      setPulseEmoji(emoji);
      setPulseCount(forCount);
      timers.set("pulse", () => setPulseEmoji(null), PULSE_MS);
      timers.set("count", () => setPulseCount(false), COUNT_MS);
    },
    [timers],
  );

  useEffect(() => timers.clearAll, [timers]);

  return { pulseEmoji, pulseCount, pulse, reset };
}

/**
 * Короткий scale-пульс при изменении счётчика.
 *
 * Первый известный счётчик только запоминается: при монтировании и при скролле
 * истории анимации нет, она возникает лишь на реальном изменении значения.
 */
function useReactionPulse(reactions: ReactionSummary[]): {
  pulsingEmojis: ReadonlySet<string>;
  pulsingCounts: ReadonlySet<string>;
} {
  const { pulseEmoji, pulseCount, pulse, reset } = usePulse();
  const previous = useRef(new Map<string, number>());
  const primed = useRef(false);

  useEffect(() => {
    const next = new Map<string, number>();
    for (const reaction of reactions) {
      if (!reaction || typeof reaction.emoji !== "string") continue;
      const key = emojiKey(reaction.emoji);
      if (key.length === 0) continue;
      const count = normaliseCount(reaction.count);
      if (count <= 0) continue;
      next.set(key, (next.get(key) ?? 0) + count);
    }

    if (!primed.current) {
      previous.current = next;
      primed.current = true;
      return;
    }

    let changed: string | null = null;
    let countChanged = false;
    for (const [key, count] of next) {
      if (previous.current.get(key) !== count) {
        changed = key;
        countChanged = true;
        break;
      }
    }
    for (const key of previous.current.keys()) {
      if (changed === null && !next.has(key)) {
        changed = key;
        countChanged = false;
        break;
      }
    }
    previous.current = next;
    if (changed !== null) pulse(changed, countChanged);
  }, [reactions, pulse]);

  /* сброс перед полной перезагрузкой истории / уходом со страницы */
  useEffect(() => {
    const element = documentRef();
    if (!element) return;
    const handleVisibility = (): void => {
      if (document.visibilityState === "hidden") reset();
    };
    element.addEventListener("visibilitychange", handleVisibility);
    return () => element.removeEventListener("visibilitychange", handleVisibility);
  }, [reset]);

  const pulsingEmojis = useMemo(
    () => (pulseEmoji === null ? new Set<string>() : new Set<string>([pulseEmoji])),
    [pulseEmoji],
  );
  const pulsingCounts = useMemo(() => {
    if (pulseEmoji === null || !pulseCount) return new Set<string>();
    return new Set<string>([pulseEmoji]);
  }, [pulseEmoji, pulseCount]);

  return { pulsingEmojis, pulsingCounts };
}

function documentRef(): Document | null {
  return typeof document === "undefined" ? null : document;
}

/* --------------------------------------------------------------- нормализация */

/** Счётчик всегда целое ≥ 0 (защита от кривых данных с сервера). */
function normaliseCount(value: number | null | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.trunc(value));
}

interface MergedReaction {
  /** Полная серверная строка эмодзи, которую отправляем обратно в PUT. */
  wire: string;
  count: number;
  me: boolean;
  operators: string[];
  operator_ids: number[];
  /** Есть ли запись ровно в серверной форме из `ALLOWED_REACTIONS`. */
  canonical: boolean;
}

/**
 * Схлопывание «дублей» по базовой графеме: «❤» и «❤️» — один чипс.
 *
 * Приоритет внутри группы:
 *  - `wire`: точная форма из контракта → самая полная (с вариативным селектором);
 *  - `canonical`: только точное совпадение с `ALLOWED_REACTIONS`, иначе `false`
 *    (пикер не подсвечивает псевдо-совпадения, хотя `onAdd` получит валидное значение).
 */
function normalizeReactions(items: ReactionSummary[] | null | undefined): ReactionSummary[] {
  if (!Array.isArray(items) || items.length === 0) return [];

  const byKey = new Map<string, MergedReaction>();

  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    if (typeof item.emoji !== "string") continue;
    const emoji = item.emoji.trim();
    if (emoji.length === 0) continue;

    const key = emojiKey(emoji);
    if (key.length === 0) continue;

    const count = normaliseCount(item.count);
    const isExactContract = (ALLOWED_REACTIONS as readonly string[]).includes(emoji);
    const merged = byKey.get(key);

    if (!merged) {
      byKey.set(key, {
        wire: emoji,
        count,
        me: item.me === true,
        operators: sanitiseNames(item.operators),
        operator_ids: sanitiseIds(item.operator_ids),
        canonical: isExactContract,
      });
      continue;
    }

    merged.count += count;
    merged.me = merged.me || item.me === true;
    merged.operators = mergeUnique(merged.operators, sanitiseNames(item.operators));
    merged.operator_ids = mergeUnique(merged.operator_ids, sanitiseIds(item.operator_ids));

    if (isExactContract) {
      merged.wire = emoji;
      merged.canonical = true;
    } else if (!merged.canonical && Array.from(emoji).length > Array.from(merged.wire).length) {
      /* «❤» без VS16 → «❤️»: чипс рисуем в полной форме */
      merged.wire = emoji;
    }
  }

  /* порядок: сначала шесть стандартных в порядке контракта, затем прочие emoji */
  const placed = new Set<string>();
  const result: ReactionSummary[] = [];
  for (const key of ALLOWED_KEYS) {
    const merged = byKey.get(key);
    if (!merged || (merged.count <= 0 && !merged.canonical)) continue;
    placed.add(key);
    result.push({
      emoji: merged.wire,
      count: merged.count,
      me: merged.me,
      operators: merged.operators,
      operator_ids: merged.operator_ids,
    });
  }
  for (const [key, merged] of byKey) {
    if (placed.has(key)) continue;
    result.push({
      emoji: merged.wire,
      count: merged.count,
      me: merged.me,
      operators: merged.operators,
      operator_ids: merged.operator_ids,
    });
  }
  return result;
}

/** `true`, если эмодзи — точная форма из контракта (для подсветки в пикере). */
function isCanonicalReaction(items: ReactionSummary[], emoji: string): boolean {
  return items.some((item) => item.me && item.emoji === emoji);
}

function sanitiseNames(names: string[] | null | undefined): string[] {
  if (!Array.isArray(names)) return [];
  const cleaned: string[] = [];
  for (const name of names) {
    if (typeof name !== "string") continue;
    const trimmed = name.trim();
    if (trimmed.length === 0) continue;
    if (!cleaned.includes(trimmed)) cleaned.push(trimmed);
  }
  return cleaned;
}

function sanitiseIds(ids: number[] | null | undefined): number[] {
  if (!Array.isArray(ids)) return [];
  const cleaned: number[] = [];
  for (const id of ids) {
    if (typeof id !== "number" || !Number.isFinite(id)) continue;
    if (!cleaned.includes(id)) cleaned.push(id);
  }
  return cleaned;
}

function mergeUnique<T>(left: T[], right: T[]): T[] {
  if (right.length === 0) return left;
  const merged = left.slice();
  for (const value of right) if (!merged.includes(value)) merged.push(value);
  return merged;
}

/** `title` чипса: «Поставили: Имя1, Имя2». */
function buildPeopleTitle(operators: readonly string[], count: number): string {
  if (operators.length === 0) {
    return count > 0 ? `Поставили: ${count}` : "Поставили";
  }
  const shown = operators.slice(0, 12);
  const hidden = operators.length - shown.length;
  const base = `Поставили: ${formatNameList(shown)}`;
  return hidden > 0 ? `${base} и ещё ${hidden}` : base;
}
/* ------------------------------------------------------------- компонент */

function ReactionBarBase({
  messageId,
  chatId,
  reactions,
  onAdd,
  onRemove,
  isOwnMessage = false,
  disabled = false,
  align = "left",
}: ReactionBarProps): JSX.Element {
  const reactId = useId().replace(/:/g, "");
  const panelId = `reaction-picker-${reactId}`;
  const rootRef = useRef<HTMLDivElement | null>(null);

  /* `messageId`/`chatId` — часть контракта: по ним контейнер (A10) строит ключи
     и адресует PUT/DELETE. Они же страхуют от переиспользования экземпляра
     между сообщениями. */
  const scopeKey = `${chatId}:${messageId}`;

  const items = useMemo(() => normalizeReactions(reactions), [reactions]);
  const mineByKey = useMemo(() => {
    const map = new Map<string, ReactionSummary>();
    for (const item of items) {
      if (item.me) map.set(emojiKey(item.emoji), item);
    }
    return map;
  }, [items]);

  const { pulsingEmojis, pulsingCounts } = useReactionPulse(items);

  /* ------------------------------ пикер ------------------------------ */

  const [isPickerOpen, setPickerOpen] = useState(false);
  const [touchDevice, setTouchDevice] = useState(detectTouchDevice);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const optionRefs = useRef(new Map<number, HTMLButtonElement>());

  const lastInteractionRef = useRef(0);
  const markInteraction = useCallback((): void => {
    lastInteractionRef.current = performance.now();
  }, []);

  /* touch / hover определяем один раз и следим за сменой условий (гибридные устройства) */
  useEffect(() => {
    if (!hasMatchMedia()) return;
    const query = window.matchMedia("(hover: none), (pointer: coarse)");
    const handleChange = (): void => setTouchDevice(detectTouchDevice());
    handleChange();
    if (typeof query.addEventListener === "function") {
      query.addEventListener("change", handleChange);
      return () => query.removeEventListener("change", handleChange);
    }
    query.addListener(handleChange);
    return () => query.removeListener(handleChange);
  }, []);

  /* уход со страницы сбрасывает панель, чтобы фокус не «улетал» */
  useEffect(() => {
    const doc = documentRef();
    if (!doc) return;
    const handleVisibility = (): void => {
      if (doc.visibilityState === "hidden") setPickerOpen(false);
    };
    doc.addEventListener("visibilitychange", handleVisibility);
    return () => doc.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  /* `disabled` гасит уже открытую панель */
  useEffect(() => {
    if (disabled) setPickerOpen(false);
  }, [disabled]);

  const closePicker = useCallback((restoreFocus: boolean): void => {
    setPickerOpen(false);
    if (restoreFocus) triggerRef.current?.focus({ preventScroll: true });
  }, []);

  /**
   * Закрытие пикера:
   *  1. `pointerdown`/`click` на `document` в фазе перехвата — любой клик вне
   *     корневого контейнера компонента;
   *  2. `Escape` на `document` — с возвратом фокуса на кнопку;
   *  3. клик по эмодзи / повторный клик по кнопке — штатные `onClick`;
   *  4. `disabled` и `visibilitychange` (уход со вкладки).
   * Слушатели живут только пока панель открыта и снимаются в cleanup;
   * события, случившиеся до открытия (тот же клик), отсекаются по `OPEN_GUARD_MS`.
   */
  useEffect(() => {
    if (!isPickerOpen) return;

    const isOutside = (target: Node): boolean =>
      !rootRef.current?.contains(target) && !panelRef.current?.contains(target);

    const handlePointer = (event: Event): void => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (performance.now() - lastInteractionRef.current < OPEN_GUARD_MS) return;
      if (!isOutside(target)) return;
      setPickerOpen(false);
    };

    const handleKeyDown = (event: KeyboardEvent): void => {
      /* Escape ловим на всём документе: фокус может быть в любом месте панели. */
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      closePicker(true);
    };

    for (const type of DOCUMENT_EVENTS) document.addEventListener(type, handlePointer, true);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      for (const type of DOCUMENT_EVENTS) document.removeEventListener(type, handlePointer, true);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isPickerOpen, closePicker]);

  /* фокус на первом эмодзи после появления панели */
  useEffect(() => {
    if (!isPickerOpen) return;
    optionRefs.current.get(0)?.focus({ preventScroll: true });
  }, [isPickerOpen]);

  const handleTriggerClick = useCallback((): void => {
    if (disabled) return;
    markInteraction();
    setPickerOpen((open) => !open);
  }, [disabled, markInteraction]);

  const handleRootKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>): void => {
      if (!isPickerOpen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closePicker(true);
        return;
      }
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      const options = PICKER_EMOJIS.length;
      const active = documentRef()?.activeElement ?? null;
      let current = -1;
      for (const [index, node] of optionRefs.current) {
        if (node === active) {
          current = index;
          break;
        }
      }
      const from = current < 0 ? 0 : current;
      const step = event.key === "ArrowRight" ? 1 : -1;
      optionRefs.current.get((from + step + options) % options)?.focus({ preventScroll: true });
    },
    [isPickerOpen, closePicker],
  );

  const handleEmojiPick = useCallback(
    (emoji: string): void => {
      if (disabled) return;
      setPickerOpen(false);
      onAdd(emoji);
    },
    [disabled, onAdd],
  );

  const handleChipClick = useCallback(
    (emoji: string): void => {
      if (disabled) return;
      /* Пикер закрываем, чтобы панель не висела над сообщением после действия. */
      setPickerOpen(false);
      onAdd(toWireEmoji(emoji));
    },
    [disabled, onAdd],
  );

  const handleChipRemove = useCallback((): void => {
    if (disabled) return;
    setPickerOpen(false);
    onRemove();
  }, [disabled, onRemove]);

  /* ------------------------------ рендер ------------------------------ */

  const triggerVisibility = disabled
    ? "opacity-50 cursor-default"
    : touchDevice || isPickerOpen
      ? "opacity-100"
      : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100";

  return (
    <div
      ref={rootRef}
      className={`relative flex min-w-0 flex-col ${align === "right" ? "items-end" : "items-start"}`}
      onKeyDown={handleRootKeyDown}
      role="toolbar"
      aria-label="Реакции на сообщение"
      aria-orientation="horizontal"
    >
      <div className="relative flex max-w-full flex-wrap items-center gap-1">
        {items.map((reaction) => {
          const key = emojiKey(reaction.emoji);
          const isMine = reaction.me === true;
          const mine = mineByKey.get(key);
          const pulsing = pulsingEmojis.has(key);
          const countPulsing = pulsingCounts.has(key);
          const label = reactionLabel(reaction.emoji);
          const peopleTitle = buildPeopleTitle(reaction.operators, reaction.count);
          const withCounter = reaction.count > 1;

          return (
            <button
              key={`${key}-${reaction.emoji}`}
              type="button"
              onClick={() => (isMine ? handleChipRemove() : handleChipClick(reaction.emoji))}
              disabled={disabled}
              aria-pressed={isMine}
              aria-label={`${label}: ${reaction.count}${isMine ? ", ваша реакция" : ""}`}
              title={`${peopleTitle} · ${isMine ? "нажмите, чтобы снять" : "нажмите, чтобы поставить"}`}
              className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-[3px] text-[11px] leading-none transition-[background-color,border-color,transform,color] duration-150 ease-tg ${
                isMine
                  ? "border-tg-accent bg-tg-accent/25 text-white"
                  : "border-transparent bg-tg-elevated text-tg-text hover:border-tg-border hover:bg-tg-hover"
              } ${pulsing ? "scale-110" : "scale-100"} ${
                disabled ? "cursor-default opacity-50" : "cursor-pointer"
              }`}
            >
              <span className="text-[13px] leading-none" aria-hidden>
                {reaction.emoji}
              </span>
              {withCounter && (
                <span
                  className={`tabular text-[10px] font-semibold ${
                    countPulsing ? "text-tg-accentHover" : isMine ? "text-white" : "text-tg-textMuted"
                  }`}
                >
                  {reaction.count}
                </span>
              )}
              {mine && mine.count > 1 && <span className="sr-only"> (включая вашу)</span>}
            </button>
          );
        })}

        <button
          ref={triggerRef}
          type="button"
          onClick={handleTriggerClick}
          disabled={disabled}
          aria-label="Поставить реакцию"
          aria-expanded={isPickerOpen}
          aria-haspopup="dialog"
          aria-controls={isPickerOpen ? panelId : undefined}
          title={disabled ? "Реакции недоступны" : "Поставить реакцию"}
          className={`inline-flex items-center rounded-full border border-transparent p-1 text-tg-textMuted transition-[opacity,background-color,color] duration-150 ease-tg hover:bg-tg-elevated hover:text-tg-accentHover ${triggerVisibility} ${
            isPickerOpen && !disabled ? "bg-tg-elevated text-tg-accentHover" : ""
          }`}
        >
          <SmilePlus size={13} aria-hidden />
        </button>

        {isPickerOpen && !disabled && (
          <div
            ref={panelRef}
            id={panelId}
            role="dialog"
            aria-label="Выбор реакции"
            className={`absolute bottom-full z-50 mb-1.5 rounded-xl border border-tg-border bg-tg-panel p-1 shadow-pop ${
              align === "right" ? "right-0" : "left-0"
            }`}
            /* Появление scale 0.95 → 1 + opacity. Инлайн-@keyframes, чтобы не
               зависеть от tailwind.config (файл принадлежит другому агенту). */
            style={{
              animation: "reaction-picker-in 140ms cubic-bezier(0.2, 0, 0.2, 1)",
              transformOrigin: align === "right" ? "bottom right" : "bottom left",
            }}
          >
            <style>{"@keyframes reaction-picker-in{from{opacity:0;transform:scale(0.95)}to{opacity:1;transform:scale(1)}}"}</style>
            <div className="flex items-center gap-0.5">
              {PICKER_EMOJIS.map((emoji, index) => {
                const label = REACTION_LABELS[emoji] ?? reactionLabel(emoji);
                const selected = isCanonicalReaction(items, emoji);
                return (
                  <button
                    key={emoji}
                    ref={(node) => {
                      if (node) optionRefs.current.set(index, node);
                      else optionRefs.current.delete(index);
                    }}
                    type="button"
                    onClick={() => handleEmojiPick(emoji)}
                    aria-label={label}
                    aria-pressed={selected}
                    title={`${label}${selected ? " (ваша реакция)" : ""}`}
                    className={`grid h-8 w-8 place-items-center rounded-lg text-[17px] leading-none transition-[background-color,transform] duration-150 ease-tg hover:scale-110 hover:bg-tg-hover focus-visible:bg-tg-hover ${
                      selected ? "bg-tg-accent/20" : ""
                    }`}
                  >
                    <span aria-hidden>{emoji}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Контейнер A10 использует эти данные для ключей и адаптивной вёрстки. */}
      <span
        className="hidden"
        aria-hidden
        data-reaction-scope={scopeKey}
        data-reaction-count={items.length}
        data-own-message={isOwnMessage ? "1" : "0"}
      />
    </div>
  );
}

export const ReactionBar = memo(ReactionBarBase);
