import { ChevronLeft, ChevronRight, ImageOff, X } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "../lib/api";
import { formatBytes } from "../lib/format";
import type { Attachment } from "../lib/types";

export interface ImageLightboxProps {
  attachments: Attachment[];
  index: number;
  onClose: () => void;
  onNavigate: (index: number) => void;
}

/** Visual media that the lightbox can show; anything else gets an info card. */
const VIEWABLE_KINDS = new Set(["PHOTO", "STICKER", "VIDEO"]);

/**
 * Absolute (token-bearing) URL for an attachment.
 *
 * The token must stay on `src`: `/api/media/{id}` answers 401 without it and the
 * viewer would show a broken image. Query flags other than `variant` are kept so
 * the original file — not the thumbnail — is requested.
 */
function mediaSrc(url: string | null | undefined): string {
  if (!url) return "";
  const withToken = typeof api.mediaUrl === "function" ? api.mediaUrl(url) : url;
  return withToken || url;
}

function fileName(attachment: Attachment): string {
  return attachment.file_name?.trim() || `Вложение #${attachment.id}`;
}

/**
 * Full-screen media viewer (CONTRACTS_V2 §6).
 *
 * Responsibilities that are easy to get wrong and are handled explicitly here:
 * - keyboard navigation with listeners removed on unmount and on prop change,
 * - `document.body` scroll lock restored to its *original* inline value,
 * - focus moved into the dialog, trapped between its own controls and returned
 *   to the element that opened it,
 * - a stable `aria-modal` dialog so screen readers ignore the chat behind it.
 */
export function ImageLightbox({ attachments, index, onClose, onNavigate }: ImageLightboxProps): JSX.Element | null {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  /* Latest `onClose` without re-subscribing the focus/scroll-lock effect: a new
     inline arrow from the parent must not tear down the scroll lock (a second
     teardown would restore the *locked* value and leave the page frozen). */
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const titleId = useId();

  const total = Array.isArray(attachments) ? attachments.length : 0;
  const safeIndex = total > 0 ? Math.min(Math.max(index, 0), total - 1) : -1;
  const current = safeIndex >= 0 ? (attachments[safeIndex] ?? null) : null;

  const goPrevious = useCallback(() => {
    if (total === 0) return;
    onNavigate((safeIndex - 1 + total) % total);
  }, [onNavigate, safeIndex, total]);

  const goNext = useCallback(() => {
    if (total === 0) return;
    onNavigate((safeIndex + 1) % total);
  }, [onNavigate, safeIndex, total]);

  /* ------------------------------------------------ focus in / out + lock */
  useEffect(() => {
    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    /* Move focus once the portal is attached (a frame later for the animation). */
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 0);

    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      const restoreTo = restoreFocusRef.current;
      if (restoreTo && document.contains(restoreTo)) {
        restoreTo.focus({ preventScroll: true });
      }
      restoreFocusRef.current = null;
    };
  }, []);

  /* ------------------------------------------------------- keyboard events */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      switch (event.key) {
        case "Escape":
          /* Stop the event so the composer's "cancel reply" handler (also bound
             to Escape) cannot run at the same time as closing the viewer. */
          event.preventDefault();
          event.stopPropagation();
          onCloseRef.current();
          return;
        case "ArrowLeft":
          event.preventDefault();
          goPrevious();
          return;
        case "ArrowRight":
          event.preventDefault();
          goNext();
          return;
        case "Tab": {
          /* Minimal focus trap inside the dialog. */
          const container = dialogRef.current;
          if (!container) return;
          const focusable = Array.from(
            container.querySelectorAll<HTMLElement>(
              'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
            ),
          ).filter((element) => element.offsetParent !== null);
          if (focusable.length === 0) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          const active = document.activeElement;
          if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
          } else if (event.shiftKey && active === first) {
            event.preventDefault();
            last.focus();
          }
          return;
        }
        default:
          return;
      }
    };

    /* Capture phase: run before any bubble-phase handler in the chat below. */
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [goNext, goPrevious]);

  /* -------------------------------------------------------- lazy prefetch */
  useEffect(() => {
    if (total === 0) return;
    const neighbours = [attachments[safeIndex - 1], attachments[safeIndex + 1]];
    for (const neighbour of neighbours) {
      if (!neighbour || !VIEWABLE_KINDS.has(neighbour.kind)) continue;
      const image = new Image();
      image.decoding = "async";
      image.src = mediaSrc(neighbour.thumbnail_url ?? neighbour.url);
    }
  }, [attachments, safeIndex, total]);

  const counter = useMemo(() => `${safeIndex + 1} / ${total}`, [safeIndex, total]);

  if (!current || total === 0) return null;
  if (typeof document === "undefined") return null;

  const isViewable = VIEWABLE_KINDS.has(current.kind);
  const previewSrc = mediaSrc(current.thumbnail_url ?? current.url);
  const fullSrc = mediaSrc(current.url);
  const downloadSrc = fullSrc || previewSrc;
  const name = fileName(current);

  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-[100] flex animate-fade-in items-center justify-center"
      style={{ backgroundColor: "rgba(0, 0, 0, 0.92)" }}
      onMouseDown={(event) => {
        /* Only a click on the backdrop itself closes; the media stops propagation. */
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <h2 id={titleId} className="sr-only">
        Просмотр вложений: {name}
      </h2>

      {/* top bar: counter + close */}
      <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex items-center justify-between gap-3 bg-gradient-to-b from-black/60 to-transparent px-3 py-2.5">
        <span className="pointer-events-auto truncate text-2xs text-white/80">{name}</span>
        <div className="pointer-events-auto flex items-center gap-2">
          <span className="rounded-full bg-black/50 px-2.5 py-1 text-2xs font-medium tabular text-white/90">
            {counter}
          </span>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="grid h-9 w-9 place-items-center rounded-full bg-black/50 text-white/90 transition hover:bg-tg-elevated hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
            aria-label="Закрыть просмотр"
            title="Закрыть (Esc)"
          >
            <X size={18} />
          </button>
        </div>
      </div>

      {/* previous */}
      {total > 1 && (
        <button
          type="button"
          onClick={goPrevious}
          className="absolute left-2 top-1/2 z-10 grid h-11 w-11 -translate-y-1/2 place-items-center rounded-full bg-black/50 text-white/90 transition hover:bg-tg-elevated hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
          aria-label="Предыдущее вложение"
          title="Предыдущее (←)"
        >
          <ChevronLeft size={22} />
        </button>
      )}

      {/* media */}
      <div
        className="flex max-h-[85vh] max-w-[90vw] animate-slide-up items-center justify-center"
        onMouseDown={(event) => event.stopPropagation()}
      >
        {isViewable && downloadSrc ? (
          <LightboxImage key={current.id} src={downloadSrc} previewSrc={previewSrc} alt={name} />
        ) : (
          <div className="flex max-w-[420px] flex-col items-center gap-2 rounded-xl border border-tg-border bg-tg-panel px-5 py-6 text-center shadow-pop">
            <ImageOff size={26} className="text-tg-textMuted" />
            <p className="text-[13px] font-medium text-tg-text">{name}</p>
            <p className="text-2xs text-tg-textFaint">
              {current.mime_type ?? "Неизвестный тип"} · {formatBytes(current.size_bytes)}
            </p>
            <p className="text-2xs text-tg-textFaint">Предпросмотр для этого типа недоступен.</p>
            {downloadSrc && (
              <a
                href={downloadSrc}
                download={name}
                target="_blank"
                rel="noreferrer noopener"
                className="mt-1 rounded-md bg-tg-accent px-3 py-1.5 text-2xs font-medium text-white transition hover:bg-tg-accentHover"
              >
                Скачать файл
              </a>
            )}
          </div>
        )}
      </div>

      {/* next */}
      {total > 1 && (
        <button
          type="button"
          onClick={goNext}
          className="absolute right-2 top-1/2 z-10 grid h-11 w-11 -translate-y-1/2 place-items-center rounded-full bg-black/50 text-white/90 transition hover:bg-tg-elevated hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
          aria-label="Следующее вложение"
          title="Следующее (→)"
        >
          <ChevronRight size={22} />
        </button>
      )}

      {/* bottom caption */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-10 flex items-center justify-center gap-2 bg-gradient-to-t from-black/60 to-transparent px-4 py-3 text-2xs text-white/70">
        <span className="truncate">
          {current.mime_type ?? "изображение"}
          {current.width && current.height ? ` · ${current.width}×${current.height}` : ""}
          {current.size_bytes !== null ? ` · ${formatBytes(current.size_bytes)}` : ""}
        </span>
      </div>
    </div>,
    document.body,
  );
}

/**
 * Image element with its own error state so a broken URL never leaves a blank overlay.
 *
 * The thumbnail is shown instantly while the full-size file decodes, then the full
 * file replaces it — a Telegram-style progressive reveal that keeps the viewer
 * responsive on slow networks.
 */
function LightboxImage({
  src,
  previewSrc,
  alt,
}: {
  src: string;
  previewSrc: string;
  alt: string;
}): JSX.Element {
  const [failed, setFailed] = useState(false);
  const [previewFailed, setPreviewFailed] = useState(false);

  if (failed) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-tg-border bg-tg-panel px-5 py-6 text-center">
        <ImageOff size={26} className="text-tg-textMuted" />
        <p className="text-[13px] text-tg-text">{alt}</p>
        <p className="text-2xs text-tg-textFaint">Не удалось загрузить изображение.</p>
      </div>
    );
  }
  return (
    <div className="relative flex items-center justify-center">
      {previewSrc && previewSrc !== src && !previewFailed && (
        <img
          src={previewSrc}
          alt=""
          aria-hidden
          draggable={false}
          onError={() => setPreviewFailed(true)}
          className="absolute max-h-[85vh] max-w-[90vw] scale-105 object-contain opacity-60 blur-md"
        />
      )}
      <img
        src={src}
        alt={alt}
        decoding="async"
        draggable={false}
        onError={() => setFailed(true)}
        className="relative max-h-[85vh] max-w-[90vw] rounded-lg object-contain shadow-pop transition-transform duration-200"
      />
    </div>
  );
}
