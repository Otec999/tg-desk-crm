import {
  Download,
  File as FileIcon,
  FileText,
  FileVideo,
  ImageOff,
  Mic,
  Music,
  Play,
} from "lucide-react";
import { memo, useMemo, useState, type ReactNode } from "react";

import { api } from "../lib/api";
import { attachmentIcon, formatBytes, formatDuration } from "../lib/format";
import type { Attachment } from "../lib/types";

export interface MediaBubbleProps {
  attachments: Attachment[];
  isOwn: boolean;
  onOpen?: (attachment: Attachment, index: number) => void;
}

/* --------------------------------------------------------------- constants */

/** Photos after this index are collapsed behind the `+N` overlay in a grid. */
const GRID_VISIBLE_LIMIT = 3;
/** Telegram-style square for the 3-up / 4-up grid cells. */
const GRID_CELL = "h-[104px] w-[104px]";

/* ------------------------------------------------------------------ helpers */

/**
 * Public URL for an attachment, with the auth token injected by `api.mediaUrl`.
 * Falls back to the raw URL when the token helper is absent (e.g. an older API
 * bundle) so a bubble never renders a broken `src`.
 */
function mediaSrc(url: string | null | undefined): string {
  if (!url) return "";
  const withToken = typeof api.mediaUrl === "function" ? api.mediaUrl(url) : url;
  return withToken || url;
}

/**
 * Safe intrinsic size for `<img width/height>`.
 *
 * A 0/negative/null dimension is not a size: `<img width={0}>` collapses the
 * element to nothing and, because the browser uses the ratio for layout, a
 * `0 × N` pair also produces a division by zero in the reserved box. Returns
 * `undefined` so the browser falls back to the natural size instead.
 */
function safeDimension(value: number | null | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.round(value) : undefined;
}

/** Display name for an attachment that may legitimately lack one. */
function displayName(attachment: Attachment, index: number): string {
  const name = attachment.file_name?.trim();
  if (name) return name;
  return `${attachment.kind || "Файл"} ${index + 1}`;
}

/** `photo.jpg` for `Content-Disposition`-style downloads. */
function downloadName(attachment: Attachment, index: number): string {
  const name = attachment.file_name?.trim();
  return name || `attachment-${attachment.id || index + 1}`;
}

/** Icon by MIME family, used by DOCUMENT / OTHER rows. */
function mimeIcon(mime: string | null, kind: string): ReactNode {
  const normalized = (mime ?? "").toLowerCase();
  if (normalized.startsWith("image/")) return <ImageOff size={16} className="text-tg-info" />;
  if (normalized.startsWith("video/")) return <FileVideo size={16} className="text-tg-danger" />;
  if (normalized.startsWith("audio/")) return <Music size={16} className="text-tg-success" />;
  if (normalized === "application/pdf" || normalized.startsWith("text/")) {
    return <FileText size={16} className="text-tg-warning" />;
  }
  if (kind === "VIDEO") return <FileVideo size={16} className="text-tg-danger" />;
  if (kind === "VOICE" || kind === "AUDIO") return <Music size={16} className="text-tg-success" />;
  if (kind === "PHOTO" || kind === "STICKER") return <ImageOff size={16} className="text-tg-info" />;
  return <FileIcon size={16} className="text-tg-textMuted" />;
}

/* ------------------------------------------------------------------ public */

/**
 * Renders every attachment of one message inside its bubble.
 *
 * Contract (CONTRACTS_V2 §3.1 / §6): `attachments` come straight from
 * `AttachmentOut`, so `kind` drives the layout and `url` / `thumbnail_url` are
 * backend-relative paths that must pass through `api.mediaUrl` before being
 * used as `src`.
 */
function MediaBubbleBase({ attachments, isOwn, onOpen }: MediaBubbleProps): JSX.Element | null {
  /* Photos render as one shared grid; every other kind renders its own row.
     The `Array.isArray` guard lives inside the memo (not only after it) so a
     malformed payload from an older backend cannot throw during render. */
  const grouped = useMemo(() => {
    const grid: Array<{ attachment: Attachment; index: number }> = [];
    const rest: Array<{ attachment: Attachment; index: number }> = [];
    if (Array.isArray(attachments)) {
      attachments.forEach((attachment, index) => {
        if (attachment.kind === "PHOTO") grid.push({ attachment, index });
        else rest.push({ attachment, index });
      });
    }
    return { grid, rest };
  }, [attachments]);

  if (!Array.isArray(attachments) || attachments.length === 0) return null;

  return (
    <div className="mb-1 flex flex-col gap-1">
      {grouped.grid.length > 0 && (
        <PhotoGrid items={grouped.grid} isOwn={isOwn} onOpen={onOpen} key="photos" />
      )}

      {grouped.rest.map(({ attachment, index }) => {
        switch (attachment.kind) {
          case "VIDEO":
            return (
              <VideoTile
                key={`video-${attachment.id}-${index}`}
                attachment={attachment}
                index={index}
                onOpen={onOpen}
              />
            );
          case "VOICE":
          case "AUDIO":
            return (
              <AudioRow
                key={`audio-${attachment.id}-${index}`}
                attachment={attachment}
                index={index}
                isOwn={isOwn}
              />
            );
          case "STICKER":
            return (
              <StickerTile
                key={`sticker-${attachment.id}-${index}`}
                attachment={attachment}
                index={index}
                onOpen={onOpen}
              />
            );
          case "DOCUMENT":
          case "OTHER":
            return (
              <FileRow
                key={`file-${attachment.id}-${index}`}
                attachment={attachment}
                index={index}
                isOwn={isOwn}
              />
            );
          default:
            /* Unknown kind from a newer backend — degrade to a file row. */
            return (
              <FileRow
                key={`unknown-${attachment.id}-${index}`}
                attachment={attachment}
                index={index}
                isOwn={isOwn}
              />
            );
        }
      })}
    </div>
  );
}

/* --------------------------------------------------------------- photo grid */

function PhotoGrid({
  items,
  isOwn,
  onOpen,
}: {
  items: Array<{ attachment: Attachment; index: number }>;
  isOwn: boolean;
  onOpen?: (attachment: Attachment, index: number) => void;
}): JSX.Element {
  const isSingle = items.length === 1;

  if (isSingle) {
    const { attachment, index } = items[0];
    return (
      <button
        type="button"
        onClick={() => onOpen?.(attachment, index)}
        className={`group/photo relative block overflow-hidden rounded-lg ${isOwn ? "bg-black/20" : "bg-black/30"}`}
        title={displayName(attachment, index)}
        aria-label={`Открыть фото ${displayName(attachment, index)}`}
      >
        <MediaImage
          attachment={attachment}
          index={index}
          className="h-auto w-auto max-h-[320px] max-w-[320px] object-cover"
        />
        <PhotoOverlay />
      </button>
    );
  }

  /* 2 photos fill a 2-column row; 3+ use a 2x2 Telegram-style grid. */
  const visible = items.length >= GRID_VISIBLE_LIMIT + 1 ? items.slice(0, 4) : items.slice(0, GRID_VISIBLE_LIMIT);
  const hiddenCount = items.length - visible.length;

  return (
    <div
      className={`grid gap-0.5 overflow-hidden rounded-lg ${isOwn ? "bg-black/20" : "bg-black/30"}`}
      style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}
    >
      {visible.map(({ attachment, index }, cellIndex) => {
        const isLast = cellIndex === visible.length - 1;
        const showMore = isLast && hiddenCount > 0;
        /* The `+N` cell is a shortcut to the first *hidden* photo, not to the one
           it is painted over: opening `index` showed the 4th picture (already
           visible) and made the "+N" affordance look broken. */
        const target = showMore ? items[visible.length] : { attachment, index };
        return (
          <button
            key={`photo-${attachment.id}-${index}`}
            type="button"
            onClick={() => onOpen?.(target.attachment, target.index)}
            className="group/photo relative block overflow-hidden"
            title={showMore ? `Ещё ${hiddenCount} фото` : displayName(attachment, index)}
            aria-label={
              showMore
                ? `Открыть ещё ${hiddenCount} фото, начиная с ${displayName(target.attachment, target.index)}`
                : `Открыть фото ${displayName(attachment, index)}`
            }
          >
            <MediaImage
              attachment={attachment}
              index={index}
              className={`${GRID_CELL} object-cover`}
            />
            {showMore ? (
              <span className="absolute inset-0 grid place-items-center bg-black/60 text-xl font-semibold text-white backdrop-blur-[1px]">
                +{hiddenCount}
              </span>
            ) : (
              <PhotoOverlay />
            )}
          </button>
        );
      })}
    </div>
  );
}

function PhotoOverlay(): JSX.Element {
  return (
    <span
      className="pointer-events-none absolute inset-0 bg-black/0 transition-colors group-hover/photo:bg-black/15"
      aria-hidden="true"
    />
  );
}

/**
 * `<img>` with lazy decoding and a graceful placeholder.
 * `onError` swaps the image for a file-name card so a dead media link still
 * communicates what the customer sent instead of showing a broken icon.
 */
function MediaImage({
  attachment,
  index,
  className,
}: {
  attachment: Attachment;
  index: number;
  className: string;
}): JSX.Element {
  const [failed, setFailed] = useState(false);
  const src = mediaSrc(attachment.thumbnail_url ?? attachment.url);
  const name = displayName(attachment, index);

  if (failed || !src) {
    return (
      <span
        className={`flex flex-col items-center justify-center gap-1 px-2 py-3 text-center text-2xs text-tg-textMuted ${className}`}
      >
        <ImageOff size={18} className="opacity-70" />
        <span className="line-clamp-2 max-w-[220px] break-all">{name}</span>
      </span>
    );
  }

  return (
    <img
      src={src}
      alt={name}
      width={safeDimension(attachment.width)}
      height={safeDimension(attachment.height)}
      loading="lazy"
      decoding="async"
      draggable={false}
      onError={() => setFailed(true)}
      className={className}
    />
  );
}

/* -------------------------------------------------------------- video tile */

function VideoTile({
  attachment,
  index,
  onOpen,
}: {
  attachment: Attachment;
  index: number;
  onOpen?: (attachment: Attachment, index: number) => void;
}): JSX.Element {
  const [failed, setFailed] = useState(false);
  const thumb = mediaSrc(attachment.thumbnail_url ?? attachment.url);
  const duration = formatDuration(attachment.duration_seconds);
  const name = displayName(attachment, index);

  return (
    <button
      type="button"
      onClick={() => onOpen?.(attachment, index)}
      className="group/video relative block w-[240px] max-w-full overflow-hidden rounded-lg bg-black/40"
      title={name}
      aria-label={`Воспроизвести видео ${name}`}
    >
      {failed || !thumb ? (
        <span className="flex h-[140px] w-full flex-col items-center justify-center gap-1 px-2 text-center text-2xs text-tg-textMuted">
          <FileVideo size={20} className="opacity-70" />
          <span className="line-clamp-2 break-all">{name}</span>
        </span>
      ) : (
        <img
          src={thumb}
          alt={name}
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => setFailed(true)}
          className="h-[140px] w-full object-cover opacity-90 transition-opacity group-hover/video:opacity-100"
        />
      )}

      <span
        className="pointer-events-none absolute inset-0 grid place-items-center"
        aria-hidden="true"
      >
        <span className="grid h-11 w-11 place-items-center rounded-full bg-black/55 text-white backdrop-blur-sm transition-transform group-hover/video:scale-105">
          <Play size={20} className="ml-0.5" fill="currentColor" />
        </span>
      </span>

      {attachment.duration_seconds !== null && attachment.duration_seconds !== undefined && (
        <span className="pointer-events-none absolute bottom-1 right-1 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-medium tabular text-white">
          {duration}
        </span>
      )}
    </button>
  );
}

/* --------------------------------------------------------------- audio row */

function AudioRow({
  attachment,
  index,
  isOwn,
}: {
  attachment: Attachment;
  index: number;
  isOwn: boolean;
}): JSX.Element {
  const [failed, setFailed] = useState(false);
  const src = mediaSrc(attachment.url);
  const isVoice = attachment.kind === "VOICE";
  const name = displayName(attachment, index);

  return (
    <div
      className={`flex w-[280px] max-w-full flex-col gap-1 rounded-lg px-2 py-1.5 ${
        isOwn ? "bg-black/20" : "bg-white/[0.05]"
      }`}
    >
      <div className="flex items-center gap-2 text-2xs">
        <span
          className={`grid h-7 w-7 shrink-0 place-items-center rounded-full ${
            isVoice ? "bg-tg-accent/25 text-tg-accentHover" : "bg-tg-info/20 text-tg-info"
          }`}
          aria-hidden="true"
        >
          {isVoice ? <Mic size={13} /> : <Music size={13} />}
        </span>
        <span className={`min-w-0 flex-1 truncate ${isOwn ? "text-white/85" : "text-tg-textMuted"}`}>
          {isVoice ? "Голосовое сообщение" : name}
        </span>
        <span className={`shrink-0 tabular ${isOwn ? "text-white/60" : "text-tg-textFaint"}`}>
          {formatDuration(attachment.duration_seconds)}
        </span>
      </div>

      {failed || !src ? (
        <span className={`text-[10px] ${isOwn ? "text-white/60" : "text-tg-textFaint"}`}>
          Аудио недоступно
        </span>
      ) : (
        <audio
          controls
          preload="none"
          src={src}
          onError={() => setFailed(true)}
          className="h-8 w-full [color-scheme:dark]"
          aria-label={`Аудио ${name}`}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------- sticker tile */

function StickerTile({
  attachment,
  index,
  onOpen,
}: {
  attachment: Attachment;
  index: number;
  onOpen?: (attachment: Attachment, index: number) => void;
}): JSX.Element {
  const [failed, setFailed] = useState(false);
  const src = mediaSrc(attachment.url);
  const name = displayName(attachment, index);

  if (failed || !src) {
    return (
      <span
        className="inline-flex h-[160px] w-[160px] flex-col items-center justify-center gap-1 text-2xs text-tg-textMuted"
        title={name}
      >
        <span className="text-2xl" aria-hidden="true">
          {attachmentIcon("STICKER")}
        </span>
        <span className="line-clamp-2 px-2 text-center break-all">{name}</span>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => onOpen?.(attachment, index)}
      className="block transition-transform hover:scale-[1.03] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-tg-accent"
      title={name}
      aria-label={`Открыть стикер ${name}`}
    >
      <img
        src={src}
        alt={name}
        width={160}
        height={160}
        loading="lazy"
        decoding="async"
        draggable={false}
        onError={() => setFailed(true)}
        className="h-[160px] w-[160px] object-contain"
      />
    </button>
  );
}

/* ---------------------------------------------------------------- file rows */

function FileRow({
  attachment,
  index,
  isOwn,
}: {
  attachment: Attachment;
  index: number;
  isOwn: boolean;
}): JSX.Element {
  const name = displayName(attachment, index);
  const href = mediaSrc(attachment.url);
  const size = formatBytes(attachment.size_bytes);

  return (
    <div
      className={`flex w-[280px] max-w-full items-center gap-2 rounded-lg px-2 py-1.5 ${
        isOwn ? "bg-black/20 hover:bg-black/30" : "bg-white/[0.05] hover:bg-white/[0.09]"
      } transition-colors`}
    >
      <span
        className={`grid h-8 w-8 shrink-0 place-items-center rounded-full ${isOwn ? "bg-black/25" : "bg-tg-elevated"}`}
        aria-hidden="true"
      >
        {mimeIcon(attachment.mime_type, attachment.kind)}
      </span>

      <span className="min-w-0 flex-1">
        <span
          className={`block truncate text-[12px] font-medium ${isOwn ? "text-white" : "text-tg-text"}`}
          title={name}
        >
          {name}
        </span>
        <span className={`block text-[10px] tabular ${isOwn ? "text-white/60" : "text-tg-textFaint"}`}>
          {attachment.kind === "OTHER" ? `${attachmentIcon("OTHER")} ${size}` : size}
        </span>
      </span>

      {href ? (
        <a
          href={href}
          download={downloadName(attachment, index)}
          target="_blank"
          rel="noreferrer noopener"
          className={`grid h-7 w-7 shrink-0 place-items-center rounded-full transition ${
            isOwn
              ? "text-white/70 hover:bg-white/15 hover:text-white"
              : "text-tg-textMuted hover:bg-tg-hover hover:text-tg-accentHover"
          }`}
          title="Скачать файл"
          aria-label={`Скачать ${name}`}
        >
          <Download size={14} />
        </a>
      ) : null}
    </div>
  );
}

export const MediaBubble = memo(MediaBubbleBase);
