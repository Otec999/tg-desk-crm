/**
 * Formatting / parsing helpers shared by every component.
 * Everything here is pure and side-effect free (easy to unit test later).
 */

/** Parse an ISO-8601 string into a Date, returning `null` for bad input. */
export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const normalized = value.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `14:05` — Telegram style HH:MM in the operator's locale. */
export function formatClock(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** `14:05:32` — precise clock used in delete badges. */
export function formatClockSeconds(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "unknown time";
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** `14:05`, `Yesterday`, `12 Mar` or `12.03.2025`. */
export function formatChatListTime(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return formatClock(value);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  if (date.getFullYear() === now.getFullYear()) {
    return date.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
  }
  return date.toLocaleDateString(undefined, { day: "2-digit", month: "2-digit", year: "numeric" });
}

/**
 * Media clock (CONTRACTS_V2 §6): `0:07`, `2:14`, `12:05`, `1:02:14`.
 * Anything unusable (null / NaN / Infinity) renders as `—` so a media bubble
 * never shows a broken label.
 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value: number): string => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(rest)}`;
  return `${minutes}:${pad(rest)}`;
}

/**
 * Human duration for SLA metrics: `45s`, `2m 14s`, `1h 02m`.
 *
 * Kept separate from {@link formatDuration} so the media clock (`2:14`) never leaks
 * into dashboard labels, where the `2m 14s` wording reads more naturally.
 */
export function formatHumanDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes < 60) return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  const pad = (value: number): string => String(value).padStart(2, "0");
  return restMinutes === 0 ? `${hours}h` : `${hours}h ${pad(restMinutes)}m`;
}

/** Size unit ladder used by {@link formatBytes}. */
const BYTE_UNITS = ["Б", "КБ", "МБ", "ГБ", "ТБ"] as const;

/**
 * Human file size in Russian: `482 Б`, `482 КБ`, `1.2 МБ`, `—`.
 *
 * Uses a binary ladder (1024) because the Telegram attachment `size_bytes`
 * field is a raw byte count. One decimal is kept below 10 units so `1.2 МБ`
 * stays informative while `147 КБ` does not become `147.3 КБ`.
 */
export function formatBytes(bytes: number | null): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  const value = Math.max(0, Math.round(bytes));
  if (value < 1024) return `${value} ${BYTE_UNITS[0]}`;

  let scaled = value;
  let unitIndex = 0;
  while (scaled >= 1024 && unitIndex < BYTE_UNITS.length - 1) {
    scaled /= 1024;
    unitIndex += 1;
  }
  const rounded = scaled >= 10 ? scaled.toFixed(0) : scaled.toFixed(1).replace(/\.0$/, "");
  return `${rounded} ${BYTE_UNITS[unitIndex]}`;
}

/** Emoji glyph per `MessageKind` — used as a compact fallback in placeholders. */
const ATTACHMENT_ICONS: Readonly<Record<string, string>> = {
  PHOTO: "📷",
  DOCUMENT: "📄",
  VIDEO: "🎬",
  VOICE: "🎤",
  AUDIO: "🎵",
  STICKER: "🎨",
  OTHER: "📎",
};

/**
 * Emoji for an attachment kind (`PHOTO` → `📷`). Unknown or missing kinds
 * degrade to the generic paperclip instead of throwing.
 */
export function attachmentIcon(kind: string): string {
  if (!kind) return ATTACHMENT_ICONS.OTHER;
  return ATTACHMENT_ICONS[kind.toUpperCase()] ?? ATTACHMENT_ICONS.OTHER;
}

/**
 * SLA countdown badge text: `MM:SS` under an hour, `H:MM:SS` above.
 * Negative values render as `-MM:SS` so overdue incidents stay readable.
 */
export function formatCountdown(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(totalSeconds)) return "--:--";
  const negative = totalSeconds < 0;
  const absolute = Math.abs(Math.round(totalSeconds));
  const hours = Math.floor(absolute / 3600);
  const minutes = Math.floor((absolute % 3600) / 60);
  const seconds = absolute % 60;
  const clock =
    hours > 0
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return negative ? `-${clock}` : clock;
}

/** Seconds remaining until an ISO timestamp, rounded to an integer. */
export function secondsUntil(value: string | null | undefined, now: number = Date.now()): number | null {
  const date = parseDate(value);
  if (!date) return null;
  return Math.round((date.getTime() - now) / 1000);
}

/** Two-letter initials for avatars: `Merchant Alpha` -> `MA`. */
export function initials(value: string): string {
  const cleaned = value.replace(/[^\p{L}\p{N}\s]/gu, " ").trim();
  if (!cleaned) return "?";
  const parts = cleaned.split(/\s+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
}

/** Stable tint per title, Telegram-style pseudo-random avatar colour. */
export function avatarColor(seed: string): string {
  const palette = ["#8774e1", "#2b5278", "#3f7a5a", "#8a5a2b", "#7a2b52", "#2b5a7a", "#5a7a2b", "#5a2b7a"];
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) % 9973;
  }
  return palette[hash % palette.length];
}

/**
 * Regex used for click-to-search / click-to-copy.
 * Matches `TX-4412-DD`, `TX-1234AB`, `tx:4412dd`, `#TX4412DD` and bare
 * `TX` + 4-12 alphanumerics, case-insensitive.
 */
export const TX_ID_REGEX = /\b(?:tx[-\s:]?#?)([0-9A-Z]{2,6}(?:-[0-9A-Z]{2,6}){0,2})\b/gi;

/** Extract the canonical (uppercased) transaction ids found in a text. */
export function extractTxIds(text: string): string[] {
  const found = new Set<string>();
  for (const match of text.matchAll(TX_ID_REGEX)) {
    const raw = match[1];
    if (!raw) continue;
    const canonical = `TX-${raw.toUpperCase().replace(/_/g, "-")}`;
    if (/^TX-[0-9A-Z-]{2,24}$/.test(canonical)) found.add(canonical);
  }
  return [...found];
}

/** True when a message body contains at least one transaction id. */
export function containsTxId(text: string): boolean {
  return extractTxIds(text).length > 0;
}

/** Normalize user input for a lookup: trim, uppercase, prefix with `TX-`. */
export function normalizeTxId(value: string): string {
  const trimmed = value.trim().toUpperCase().replace(/\s+/g, "");
  if (!trimmed) return "";
  if (trimmed.startsWith("TX-")) return trimmed;
  if (trimmed.startsWith("TX")) return `TX-${trimmed.slice(2)}`;
  return `TX-${trimmed}`;
}

/**
 * First line of a message collapsed to a single-line preview.
 *
 * `value` is typed `string` but is fed straight from the wire, where a row ingested
 * before the column became `NOT NULL` can still be `NULL`: collapsing it here keeps
 * one bad row from throwing inside a render and blanking the whole composer.
 */
export function previewText(value: string | null | undefined, limit = 90): string {
  const flat = (value ?? "").replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

/** RFC-4122 v4 uuid, with a synchronous fallback for non-secure contexts. */
export function uuid(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
  const bytes = new Uint8Array(16);
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
