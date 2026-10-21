"""Media downloader for the TG-Desk Telethon worker.

Responsibilities (CONTRACTS_V2.md §2.1 + §5):

* download one attachment of an incoming Telegram message into
  ``{media_root}/chats/{chat_id}/{tg_message_id}/``;
* classify it into a :class:`MediaKind` value (``PHOTO`` / ``DOCUMENT`` /
  ``VOICE`` / ``VIDEO`` / ``STICKER`` / ``AUDIO`` / ``OTHER``);
* produce a JPEG thumbnail (320 px wide) for photos and videos;
* report a **POSIX-style** path relative to ``media_root`` so the database
  never depends on the operating system of the host.

Design rules
------------
* The module never raises out of :func:`download_media`: every failure is
  logged and mapped to ``None`` so a broken attachment can never take the
  long-lived worker process down.
* Oversized files are **not** downloaded: a record with ``storage_path=None``
  is returned so the UI can render "файл слишком большой".
* File names coming from Telegram are attacker-controlled: they are
  sanitised (no traversal, no separators, no control characters, ≤120 chars)
  and every write is verified to stay inside the download directory.
* Pillow is optional. Without it thumbnails are skipped with a warning.

Everything that touches Telethon is isolated behind small helpers taking
already-constructed ``telethon.tl.types`` objects, and every public function
takes the ``client`` explicitly, which keeps the unit tests free of network
calls (``telethon_worker/test_media.py``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import os
import re
import tempfile
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final, Iterable, Optional

# Pillow is optional: the worker stays functional (without previews) on a
# machine where the wheel is unavailable.
try:  # pragma: no cover - exercised by the environment, not by tests
    from PIL import Image, ImageOps

    PILLOW_AVAILABLE: bool = True
    _PILLOW_IMPORT_ERROR: Optional[str] = None
except Exception as _pillow_exc:  # pragma: no cover - optional dependency
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False
    _PILLOW_IMPORT_ERROR = f"{type(_pillow_exc).__name__}: {_pillow_exc}"

LOG = logging.getLogger("tg_worker.media")


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Default size ceiling for a single attachment (50 MiB). Overridable via
#: ``MEDIA_MAX_DOWNLOAD_BYTES`` / ``MAX_DOWNLOAD_BYTES`` or the config object.
MAX_DOWNLOAD_BYTES: Final[int] = 50 * 1024 * 1024

#: Thumbnail width in pixels (height keeps the aspect ratio).
THUMBNAIL_WIDTH: Final[int] = 320

#: Maximum length of a sanitised file name, extension included.
MAX_FILE_NAME_LENGTH: Final[int] = 120

#: Fallback stem when Telegram reports nothing usable.
DEFAULT_FILE_STEM: Final[str] = "file"

#: Default extension per kind when neither the file name nor the mime type
#: gives one away (Telegram photos/stickers often have no ``file_name``).
DEFAULT_EXTENSION_BY_KIND: Final[dict[str, str]] = {
    "PHOTO": ".jpg",
    "VIDEO": ".mp4",
    "VOICE": ".ogg",
    "AUDIO": ".mp3",
    "STICKER": ".webp",
    "DOCUMENT": ".bin",
    "OTHER": ".bin",
}

#: Characters that are never allowed in a stored file name. Guards against
#: NTFS alternate data streams, globbing and shell metacharacters.
_FORBIDDEN_CHARS: Final[str] = '<>:"/\\|?*'

#: Windows reserved device names (case-insensitive, with or without extension).
_WINDOWS_RESERVED: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

#: Mime prefixes/value -> kind (documents only; used when Telethon did not
#: give us a specialised attribute such as ``Voice``/``Sticker``).
_MIME_KIND_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("image/gif", "OTHER"),          # animations (GIF) are not "photos"
    ("video/", "VIDEO"),
    ("audio/ogg", "VOICE"),
    ("audio/x-ogg", "VOICE"),
    ("audio/opus", "VOICE"),
    ("audio/", "AUDIO"),
    ("image/", "PHOTO"),
)

#: Extensions that identify an animation even when Telegram calls it a video.
_ANIMATION_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".gif", ".webp", ".apng", ".avifs"}
)

#: Telegram video messages may carry no ``mime_type`` at all.
_VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"}
)
_AUDIO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp3", ".m4a", ".flac", ".wav", ".aac", ".opus", ".wma"}
)
_IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".tif", ".tiff"}
)
_VOICE_EXTENSIONS: Final[frozenset[str]] = frozenset({".oga", ".ogg", ".opus"})

#: Telethon converts an extensionless photo to JPEG before uploading.
_PHOTO_MIME_GUESS: Final[str] = "image/jpeg"


class MediaKind(StrEnum):
    """Mirror of ``backend.app.enums.MessageKind`` (CONTRACTS_V2 §1).

    Duplicated on purpose: the worker runs as a standalone process from
    ``telethon_worker/`` and must never import the backend package.
    """

    TEXT = "TEXT"
    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    VOICE = "VOICE"
    VIDEO = "VIDEO"
    STICKER = "STICKER"
    AUDIO = "AUDIO"
    OTHER = "OTHER"


# --------------------------------------------------------------------------- #
# Pure helpers — file names
# --------------------------------------------------------------------------- #

_WINDOWS_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_UNSAFE_DOTS_RE = re.compile(r"\.{2,}")
_EMPTY_BRACKETS_RE = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")


def sanitize_file_name(raw: str, *, default_stem: str = DEFAULT_FILE_STEM) -> str:
    """Make an untrusted Telegram file name safe to store **and** to display.

    Transformations, in order:

    1. ``..`` collapses to ``.`` (no parent-directory traversal);
    2. path separators and control characters are removed, on top of the
       Windows-illegal set ``<>:"/\\|?*`` (one sanitised name works on every
       host OS);
    3. all Unicode whitespace is normalised to single spaces, trailing dots
       and spaces are stripped (Windows silently drops them);
    4. the stem is clipped so that ``stem + extension`` fits
       :data:`MAX_FILE_NAME_LENGTH` (120) characters;
    5. reserved Windows device names are prefixed with ``_``;
    6. an empty result degrades to ``default_stem``.

    Args:
        raw: The ``document.file_name`` / derived name from Telegram.
        default_stem: Used when nothing usable is left.

    Returns:
        A sanitised file name (never empty, never containing a separator).
    """
    if raw is None:
        raw = ""
    name = str(raw).replace("\x00", "")
    # Collapse line breaks / tabs before they hit the whitespace normaliser.
    name = name.strip()

    # 1) traversal
    name = _UNSAFE_DOTS_RE.sub(".", name)
    # 2) separators + control characters (Windows and POSIX alike)
    name = _WINDOWS_ILLEGAL_RE.sub("_", name)
    # 3) whitespace
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()

    if not name:
        return _safe_stem(default_stem) or DEFAULT_FILE_STEM

    suffix = PurePosixPath(name).suffix
    # A "suffix" longer than 16 chars is not an extension ("file.name.is.here"
    # reports ".here"; "Something.That.Looks.Like.Version" must not be cut).
    if len(suffix) > 16:
        suffix = ""
    stem = name[: len(name) - len(suffix)] if suffix else name
    stem = _safe_stem(stem)
    suffix = suffix.strip().strip(".")
    suffix = re.sub(r"[^0-9A-Za-z_\-]", "", suffix).lower()
    suffix = f".{suffix}" if suffix else ""

    if not stem:
        stem = _safe_stem(default_stem) or DEFAULT_FILE_STEM

    budget = MAX_FILE_NAME_LENGTH - len(suffix)
    if budget < 1:
        # Pathological extension: drop it rather than producing "file.".
        suffix = ""
        budget = MAX_FILE_NAME_LENGTH
    stem = stem[:budget].strip().strip(".").strip() or DEFAULT_FILE_STEM[:budget]

    return f"{stem}{suffix}"


def _safe_stem(stem: str) -> str:
    """Sanitise the stem part, honouring Windows device names."""
    cleaned = _EMPTY_BRACKETS_RE.sub("", stem).strip().strip(".").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""
    if cleaned.upper() in {name.upper() for name in _WINDOWS_RESERVED}:
        cleaned = f"_{cleaned}"
    return cleaned


def ensure_extension(file_name: str, *, kind: str, mime_type: Optional[str]) -> str:
    """Give ``file_name`` an extension derived from ``mime_type``/``kind``.

    A name that already carries a plausible extension is returned untouched,
    so the downloader can trust it when deriving the on-disk path.
    """
    if PurePosixPath(file_name).suffix:
        return file_name
    guess = mimetypes.guess_extension((mime_type or "").split(";")[0].strip()) or ""
    if guess == ".jpe":  # mimetypes' odd preference for image/jpeg
        guess = ".jpg"
    if not guess:
        guess = DEFAULT_EXTENSION_BY_KIND.get(kind, ".bin")
    return sanitize_file_name(f"{file_name}{guess}")


def resolve_storage_path(
    storage_path: Optional[str], media_root: Path
) -> Optional[Path]:
    """Turn a stored POSIX-relative path into an absolute path.

    Used by the backend/tests to locate a file; returns ``None`` for a
    missing or unsafe (absolute / traversing) value.
    """
    if not storage_path:
        return None
    candidate = PurePosixPath(str(storage_path).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return Path(media_root).joinpath(*candidate.parts)


# --------------------------------------------------------------------------- #
# Pure helpers — classification
# --------------------------------------------------------------------------- #

def _first_attr(message: Any, *names: str) -> Any:
    """Return the first non-``None`` attribute of ``message`` (never raises)."""
    for name in names:
        try:
            value = getattr(message, name, None)
        except Exception:  # pragma: no cover - exotic proxies
            value = None
        if value is not None:
            return value
    return None


def document_attribute(message: Any, attribute_name: str) -> Any:
    """Return a ``DocumentAttribute*`` instance (by class name) of the message.

    Telethon exposes only some attributes as message properties
    (``.photo``, ``.voice``, ``.sticker`` …); the rest (file name, size,
    duration, dimensions) live inside ``media.document.attributes``. This
    helper accepts both real Telethon objects and plain test doubles.
    """
    media = _first_attr(message, "media", "document")
    if media is None:
        return None
    document = _first_attr(media, "document") or media
    attributes: Iterable[Any] = ()
    try:
        attributes = getattr(document, "attributes", None) or ()
    except Exception:  # pragma: no cover - defensive
        attributes = ()
    for attribute in attributes:
        if type(attribute).__name__ == attribute_name:
            return attribute
    return None


def detect_kind(
    message: Any,
    *,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> str:
    """Classify a Telethon message into a :class:`MediaKind` value.

    Resolution order (first hit wins):

    1. ``message.photo`` → ``PHOTO``;
    2. ``message.video_note`` / ``message.video`` → ``VIDEO``;
    3. ``message.voice`` → ``VOICE``;
    4. ``message.audio`` → ``AUDIO``;
    5. ``message.sticker`` → ``STICKER``;
    6. ``message.gif`` → ``OTHER`` (animation by contract);
    7. ``message.document`` / ``MessageMediaDocument`` → refined with the
       document attributes (voice flag, round message, animated flag,
       sticker) and then with ``mime_type`` / extension;
    8. no media at all → ``TEXT``.

    Photographs and real videos are always placed **before** the document
    branch, because Telethon sets ``.document`` for every file-backed media.

    Args:
        message: A ``telethon.tl.custom.Message`` or a stand-in with the same
            attribute names.
        mime_type: Overrides the document's ``mime_type`` (used by tests and
            when the caller already extracted it).
        file_name: Used to spot GIF/animation documents without a mime type.

    Returns:
        One of the ``MediaKind`` values as a plain string.
    """
    try:
        if _first_attr(message, "photo") is not None:
            return MediaKind.PHOTO.value
        if _first_attr(message, "video_note") is not None:
            return MediaKind.VIDEO.value
        if _first_attr(message, "voice") is not None:
            return MediaKind.VOICE.value
        if _first_attr(message, "video") is not None:
            return MediaKind.VIDEO.value
        if _first_attr(message, "audio") is not None:
            return MediaKind.AUDIO.value
        if _first_attr(message, "sticker") is not None:
            return MediaKind.STICKER.value
        if _first_attr(message, "gif") is not None:
            return MediaKind.OTHER.value

        document = _first_attr(message, "document")
        media = _first_attr(message, "media")
        is_document_media = document is not None or (
            media is not None and type(media).__name__ == "MessageMediaDocument"
        )
        if not is_document_media and media is None:
            return MediaKind.TEXT.value

        attributes = list(getattr(document, "attributes", None) or ()) if document is not None else []
        for attribute in attributes:
            name = type(attribute).__name__
            if name == "DocumentAttributeAudio":
                return (
                    MediaKind.VOICE.value
                    if getattr(attribute, "voice", False)
                    else MediaKind.AUDIO.value
                )
            if name == "DocumentAttributeVideo":
                return (
                    MediaKind.VIDEO.value
                    if getattr(attribute, "round_message", False)
                    else MediaKind.VIDEO.value
                )
            if name == "DocumentAttributeSticker":
                return MediaKind.STICKER.value
            if name == "DocumentAttributeAnimated":
                return MediaKind.OTHER.value

        effective_mime = (mime_type or getattr(document, "mime_type", None) or "").split(";")[0].strip().lower()
        effective_name = file_name or _document_file_name(document) or ""
        extension = PurePosixPath(effective_name).suffix.lower()

        for prefix, kind in _MIME_KIND_MAP:
            if effective_mime.startswith(prefix):
                return MediaKind(kind).value
        if extension in _ANIMATION_EXTENSIONS:
            return MediaKind.OTHER.value
        if extension in _VIDEO_EXTENSIONS:
            return MediaKind.VIDEO.value
        if extension in _VOICE_EXTENSIONS and effective_mime.startswith("audio/"):
            return MediaKind.VOICE.value
        if extension in _AUDIO_EXTENSIONS:
            return MediaKind.AUDIO.value
        if extension in _IMAGE_EXTENSIONS:
            return MediaKind.PHOTO.value
        if not is_document_media and media is not None:
            # Polls, geo, contacts, web pages — nothing to download.
            return MediaKind.OTHER.value
        return MediaKind.DOCUMENT.value
    except Exception:  # pragma: no cover - never fail classification
        LOG.debug("message kind detection failed; falling back to OTHER", exc_info=True)
        return MediaKind.OTHER.value


def _document_file_name(document: Any) -> Optional[str]:
    """Extract ``DocumentAttributeFilename.file_name`` without raising."""
    if document is None:
        return None
    for attribute in getattr(document, "attributes", None) or ():
        if type(attribute).__name__ == "DocumentAttributeFilename":
            name = getattr(attribute, "file_name", None)
            if name:
                return str(name)
    return None


def guess_file_name(message: Any, *, kind: str, mime_type: Optional[str]) -> str:
    """Human-friendly file name for an attachment (sanitised, with extension)."""
    document = _first_attr(message, "document")
    candidate = _document_file_name(document)
    if not candidate:
        for attribute_name in ("photo", "video", "voice", "video_note", "audio", "sticker"):
            media: Any = None
            try:
                media = getattr(message, attribute_name, None)
            except Exception:  # pragma: no cover - defensive
                media = None
            if media is None:
                continue
            if kind == MediaKind.STICKER.value:
                candidate = f"sticker_{getattr(media, 'id', '0')}"
            elif kind == MediaKind.VOICE.value:
                candidate = f"voice_{getattr(media, 'id', '0')}"
            elif kind == MediaKind.VIDEO.value and getattr(message, "video_note", None) is not None:
                candidate = f"video_message_{getattr(media, 'id', '0')}"
            else:
                candidate = f"{kind.lower()}_{getattr(media, 'id', '0')}"
            break
        else:
            media = _first_attr(message, "media")
            media_id = getattr(media, "id", None) or getattr(document, "id", None) or "0"
            candidate = f"{kind.lower()}_{media_id}"
    stem = sanitize_file_name(candidate)
    return ensure_extension(stem, kind=kind, mime_type=mime_type)


def guess_mime_type(message: Any, *, kind: str, file_name: str) -> Optional[str]:
    """Best-effort mime type (document mime > mimetypes > kind defaults)."""
    document = _first_attr(message, "document")
    mime = getattr(document, "mime_type", None) if document is not None else None
    if mime:
        return str(mime)
    if kind == MediaKind.PHOTO.value:
        return _PHOTO_MIME_GUESS
    guessed, _encoding = mimetypes.guess_type(file_name)
    if guessed:
        return guessed
    return {
        MediaKind.VIDEO.value: "video/mp4",
        MediaKind.VOICE.value: "audio/ogg",
        MediaKind.AUDIO.value: "audio/mpeg",
        MediaKind.STICKER.value: "image/webp",
    }.get(kind)


def extract_size_bytes(message: Any) -> Optional[int]:
    """Attachment size in bytes, if Telegram reported one."""
    document = _first_attr(message, "document")
    size = getattr(document, "size", None) if document is not None else None
    if size is None:
        media = _first_attr(message, "media")
        size = getattr(media, "size", None) if media is not None else None
        if size is None:
            photo = _first_attr(media, "photo") if media is not None else None
            sizes = getattr(photo, "sizes", None) or ()
            best = 0
            for size_obj in sizes:
                best = max(best, int(getattr(size_obj, "size", 0) or 0))
            size = best or None
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def extract_dimensions(message: Any) -> tuple[Optional[int], Optional[int]]:
    """``(width, height)`` from the photo candidate or video attributes."""
    photo = _first_attr(message, "photo")
    if photo is not None:
        best: tuple[int, int, int] = (0, 0, 0)  # (area, w, h)
        for size_obj in getattr(photo, "sizes", None) or ():
            width = int(getattr(size_obj, "w", 0) or 0)
            height = int(getattr(size_obj, "h", 0) or 0)
            if width > 0 and height > 0 and width * height > best[0]:
                best = (width * height, width, height)
        if best[0]:
            return best[1], best[2]
        return None, None

    for attribute_name in (
        "DocumentAttributeVideo",
        "DocumentAttributeImageSize",
        "DocumentAttributeSticker",
    ):
        attribute = document_attribute(message, attribute_name)
        if attribute is None:
            continue
        width = getattr(attribute, "w", None)
        height = getattr(attribute, "h", None)
        if width is None:
            width = getattr(attribute, "width", None)
        if height is None:
            height = getattr(attribute, "height", None)
        if width or height:
            return _positive_int(width), _positive_int(height)
    return None, None


def extract_duration_seconds(message: Any) -> Optional[int]:
    """Duration in whole seconds from an audio/video document attribute."""
    attribute = document_attribute(message, "DocumentAttributeVideo")
    duration = getattr(attribute, "duration", None) if attribute is not None else None
    if duration is None:
        for attribute_name in ("DocumentAttributeAudio", "DocumentAttributeVideo"):
            attribute = document_attribute(message, attribute_name)
            if attribute is None:
                continue
            duration = getattr(attribute, "duration", None)
            if duration is not None:
                break
    if duration is None:
        media = _first_attr(message, "media")
        duration = getattr(media, "duration", None) if media is not None else None
    try:
        seconds = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        return None
    if seconds is None or seconds <= 0 or math.isnan(seconds):
        return None
    return int(round(seconds))


def extract_tg_file_id(message: Any) -> Optional[str]:
    """Stable reference to the Telegram file: ``"{type}/{id}/{access_hash}"``.

    ``file_reference`` is deliberately **not** included: the column is
    ``VARCHAR(255)`` (CONTRACTS_V2 §2.1) and its hex form can overflow it,
    which would surface as an ``IntegrityError`` inside the backend. The
    reference is only needed to re-download a file later, and the worker
    downloads every attachment immediately upon receipt — for the CRM the
    value is informational (``photo/1234/5678``, ``document/987/654``).

    Session-scoped anyway: ``access_hash`` may be invalidated by Telegram.
    """
    document = _first_attr(message, "document")
    media = _first_attr(message, "media")
    photo = _first_attr(message, "photo")

    file_id = getattr(document, "id", None) if document is not None else None
    access_hash = getattr(document, "access_hash", None) if document is not None else None
    kind = "document"

    if photo is not None:
        photo_id = getattr(photo, "id", None)
        if photo_id is not None:
            file_id = photo_id
            access_hash = getattr(photo, "access_hash", None)
            kind = "photo"
    if file_id is None and media is not None:
        file_id = getattr(media, "id", None)
        access_hash = getattr(media, "access_hash", None)
        kind = "media"
    if file_id is None and access_hash is None:
        return None

    return "/".join([kind, str(file_id or "0"), str(access_hash or "0")])


def _positive_int(value: Any) -> Optional[int]:
    """``int(value)`` when positive, ``None`` otherwise (never raises)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------- #
# Pure helpers — paths
# --------------------------------------------------------------------------- #

def resolve_media_root(media_root: Any) -> Path:
    """Normalise ``media_root`` to an absolute ``Path``."""
    return Path(media_root).expanduser().resolve()


def chat_storage_dir(media_root: Path, chat_id: int) -> Path:
    """``{media_root}/chats/{chat_id}`` — the per-chat folder."""
    return resolve_media_root(media_root) / "chats" / str(int(chat_id))


def message_storage_dir(media_root: Path, chat_id: int, tg_message_id: int) -> Path:
    """``{media_root}/chats/{chat_id}/{tg_message_id}`` — the per-message folder."""
    return chat_storage_dir(media_root, chat_id) / str(int(tg_message_id))


def to_posix_relative(path: Path, media_root: Path) -> str:
    """Render ``path`` relative to ``media_root`` with forward slashes.

    The database must not depend on the host OS, so a file stored on Windows
    as ``…\\storage\\media\\chats\\-100…\\42\\photo.jpg`` is persisted as
    ``chats/-100…/42/photo.jpg``. Raises :class:`ValueError` when ``path``
    escapes ``media_root`` (a bug, never valid input).
    """
    root = resolve_media_root(media_root)
    absolute = Path(path).resolve()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"path {absolute} is outside media_root {root}"
        ) from exc
    return PurePosixPath(*relative.parts).as_posix()


def unique_path(directory: Path, file_name: str) -> Path:
    """Return a non-existing path inside ``directory``.

    Existing files are never overwritten. An identical name gets the Telegram
    message-id suffix the folder already provides, then ``-1``, ``-2`` …
    Telegram can legitimately send two attachments with the same name
    (e.g. two ``IMG_0001.jpg``), and silently replacing one loses data.
    """
    candidate = directory / file_name
    if not candidate.exists():
        return candidate
    stem = PurePosixPath(file_name).stem
    suffix = PurePosixPath(file_name).suffix
    for index in range(1, 1000):
        alternative = directory / f"{stem}-{index}{suffix}"
        if not alternative.exists():
            return alternative
    # Pathological (>999 duplicates): use a random-ish tail to stay unique.
    handle, temp_name = tempfile.mkstemp(dir=directory, prefix=f"{stem}-", suffix=suffix)
    os.close(handle)
    candidate = Path(temp_name)
    return candidate


def ensure_inside(path: Path, root: Path) -> Path:
    """Verify ``path`` stays inside ``root`` (defence in depth for writes).

    Raises:
        ValueError: when the resolved path escapes ``root``.
    """
    resolved_root = Path(root).resolve()
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"refusing to write outside {resolved_root}: {resolved}") from exc
    return resolved


# --------------------------------------------------------------------------- #
# Thumbnails (Pillow, optional)
# --------------------------------------------------------------------------- #

def make_thumbnail(
    source: Path,
    *,
    width: int = THUMBNAIL_WIDTH,
) -> Optional[Path]:
    """Create ``{source}.thumb.jpg`` (``width`` px wide) from ``source``.

    Returns:
        The thumbnail path, or ``None`` when Pillow is missing, the file
        cannot be decoded (documents, corrupt images) or the write failed.
        Failures are logged but **never** raised: a missing preview must not
        lose an otherwise downloaded attachment.
    """
    if not PILLOW_AVAILABLE:
        LOG.warning(
            "Pillow is not installed (%s) — thumbnails disabled. "
            "Run: pip install -r telethon_worker/requirements.txt",
            _PILLOW_IMPORT_ERROR or "import failed",
        )
        return None

    thumb_path = source.parent / f"{source.name}.thumb.jpg"
    try:
        if not source.is_file():
            return None
        with Image.open(source) as opened:  # type: ignore[union-attr]
            try:
                opened.seek(0)  # first frame of an animation
            except (EOFError, ValueError, OSError):
                pass
            image = ImageOps.exif_transpose(opened)  # type: ignore[union-attr]
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            if image.width > width:
                ratio = width / float(image.width)
                target_height = max(1, int(round(image.height * ratio)))
                image = image.resize((width, target_height), Image.LANCZOS)  # type: ignore[union-attr]
            temporary = thumb_path.with_suffix(thumb_path.suffix + ".part")
            image.save(temporary, format="JPEG", quality=82, optimize=True)
        os.replace(temporary, thumb_path)
        return thumb_path
    except Exception as exc:
        LOG.warning("thumbnail generation failed for %s: %s: %s",
                    source.name, type(exc).__name__, exc)
        try:
            temporary.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except (OSError, UnboundLocalError):
            pass
        return None


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def _media_record(
    *,
    kind: str,
    file_name: str,
    mime_type: Optional[str],
    size_bytes: Optional[int],
    width: Optional[int],
    height: Optional[int],
    duration_seconds: Optional[int],
    storage_path: Optional[str],
    tg_file_id: Optional[str],
    thumbnail_path: Optional[str],
) -> dict[str, Any]:
    """Build the exact record shape published on ``tg_incoming.media[]``."""
    return {
        "kind": kind,
        "file_name": file_name,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "duration_seconds": duration_seconds,
        "storage_path": storage_path,
        "tg_file_id": tg_file_id,
        "thumbnail_path": thumbnail_path,
    }


def _has_downloadable_media(message: Any) -> bool:
    """True when the message carries an attachment we could fetch."""
    try:
        if getattr(message, "media", None) is None:
            return False
        kind = detect_kind(message)
        if kind in (MediaKind.TEXT.value, MediaKind.OTHER.value):
            # OTHER is an animation (GIF): Telegram delivers it as a document,
            # so it is still downloadable.
            return kind == MediaKind.OTHER.value and (
                getattr(message, "document", None) is not None
                or getattr(message, "gif", None) is not None
            )
        return True
    except Exception:  # pragma: no cover - defensive
        return False


async def download_media(
    client: Any,
    message: Any,
    media_root: Path,
    chat_id: int,
    *,
    max_download_bytes: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Download the attachment of ``message`` into the media store.

    Destination: ``{media_root}/chats/{chat_id}/{tg_message_id}/{file_name}``.

    Args:
        client: A Telethon ``TelegramClient`` (only ``download_media`` is
            used — test doubles implement the same coroutine).
        message: ``telethon.tl.custom.Message`` or an object exposing the same
            attributes (``media``, ``photo``, ``document``, ``voice`` …).
        media_root: Root of the media store (``settings.media_root``).
        chat_id: Telegram chat id (the marked ``-100…`` form).
        max_download_bytes: Size ceiling. When the attachment is larger it is
            **not** downloaded and a record with ``storage_path=None`` is
            returned so the UI can show "файл слишком большой".

    Returns:
        A dict with the keys
        ``kind, file_name, mime_type, size_bytes, width, height,
        duration_seconds, storage_path, tg_file_id, thumbnail_path``
        (``storage_path`` is POSIX-relative to ``media_root``) — or ``None``
        when the message has no downloadable attachment.

    Notes:
        Every exception is logged and mapped to ``None``; the worker's event
        loop must never see a failed download as a crash.
    """
    if message is None:
        return None

    # -- classification (pure, cannot fail the process) -------------------- #
    try:
        kind = detect_kind(message)
    except Exception:
        LOG.exception("media: kind detection crashed — message ignored")
        return None

    if not _has_downloadable_media(message):
        LOG.debug("media: message %s has no downloadable attachment (kind=%s)",
                  getattr(message, "id", "?"), kind)
        return None

    tg_message_id = _positive_int(getattr(message, "id", None))
    if tg_message_id is None:
        LOG.warning("media: message without id (chat=%s) — skipped", chat_id)
        return None

    limit = MAX_DOWNLOAD_BYTES if max_download_bytes is None else int(max_download_bytes)
    if limit <= 0:
        limit = MAX_DOWNLOAD_BYTES

    mime_type: Optional[str] = None
    file_name = DEFAULT_FILE_STEM
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[int] = None
    tg_file_id: Optional[str] = None
    try:
        file_name = guess_file_name(message, kind=kind, mime_type=None)
        mime_type = guess_mime_type(message, kind=kind, file_name=file_name)
        size_bytes = extract_size_bytes(message)
        width, height = extract_dimensions(message)
        duration_seconds = extract_duration_seconds(message)
        tg_file_id = extract_tg_file_id(message)
    except Exception:
        LOG.exception("media: metadata extraction failed for msg=%s (continuing with defaults)",
                      tg_message_id)

    # -- size guard: never download what we are going to reject ----------- #
    if size_bytes is not None and size_bytes > limit:
        LOG.warning(
            "media: attachment too large (msg=%s name=%s size=%s > %s) — not downloaded",
            tg_message_id, file_name, size_bytes, limit,
        )
        return _media_record(
            kind=kind, file_name=file_name, mime_type=mime_type,
            size_bytes=size_bytes, width=width, height=height,
            duration_seconds=duration_seconds, storage_path=None,
            tg_file_id=tg_file_id, thumbnail_path=None,
        )

    directory = message_storage_dir(media_root, chat_id, tg_message_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = ensure_inside(unique_path(directory, file_name), resolve_media_root(media_root))
    except (OSError, ValueError) as exc:
        LOG.error("media: cannot prepare directory for msg=%s: %s: %s",
                  tg_message_id, type(exc).__name__, exc)
        return None

    # -- download (Telethon handles chunking, retries and CDN redirects) -- #
    downloaded: Any = None
    try:
        downloader = getattr(client, "download_media", None)
        if downloader is None or not callable(downloader):
            LOG.error("media: client has no download_media(); msg=%s skipped", tg_message_id)
            return None
        # Telethon 1.37's ``download_media`` accepts only ``file``, ``thumb`` and
        # ``progress_callback``; passing ``file_size`` raises TypeError. The size
        # ceiling is therefore enforced above via ``extract_size_bytes``.
        options: dict[str, Any] = {"file": str(target)}
        downloaded = await downloader(message, **options)
    except asyncio.CancelledError:
        raise  # shutdown must keep propagating
    except Exception as exc:
        LOG.error("media: download failed (msg=%s name=%s): %s: %s",
                  tg_message_id, file_name, type(exc).__name__, exc)
        _cleanup_partial(target)
        return None

    if not downloaded:
        LOG.error("media: Telegram returned no file for msg=%s (name=%s)", tg_message_id, file_name)
        _cleanup_partial(target)
        return None

    actual_path = Path(downloaded)
    try:
        actual_path = ensure_inside(actual_path, resolve_media_root(media_root))
    except ValueError as exc:
        LOG.error("media: downloader returned an unsafe path (%s) — discarded", exc)
        _cleanup_partial(actual_path)
        return None

    try:
        actual_size = actual_path.stat().st_size
    except OSError as exc:
        LOG.error("media: downloaded file is not readable (%s): %s: %s",
                  actual_path.name, type(exc).__name__, exc)
        return None

    # A photo/document may report no size upfront: enforce the ceiling here.
    if actual_size > limit:
        LOG.warning("media: downloaded file exceeds limit (msg=%s size=%s > %s) — removed",
                    tg_message_id, actual_size, limit)
        _cleanup_partial(actual_path)
        return _media_record(
            kind=kind, file_name=actual_path.name, mime_type=mime_type,
            size_bytes=actual_size, width=width, height=height,
            duration_seconds=duration_seconds, storage_path=None,
            tg_file_id=tg_file_id, thumbnail_path=None,
        )

    # -- thumbnail (photos and videos only) ------------------------------- #
    thumbnail_relative: Optional[str] = None
    thumbnail_absolute: Optional[Path] = None
    if kind in (MediaKind.PHOTO.value, MediaKind.VIDEO.value):
        thumbnail_absolute = make_thumbnail(actual_path)
        if thumbnail_absolute is not None:
            try:
                thumbnail_relative = to_posix_relative(thumbnail_absolute, media_root)
            except ValueError as exc:
                LOG.warning("media: thumbnail outside media_root (%s) — dropped", exc)
                thumbnail_absolute = None

    # -- dimensions fallback: read them from the decoded image ------------ #
    if width is None or height is None:
        image_width, image_height = _probe_image_size(actual_path, kind=kind)
        width = width if width is not None else image_width
        height = height if height is not None else image_height

    try:
        storage_path = to_posix_relative(actual_path, media_root)
    except ValueError as exc:
        LOG.error("media: stored file escaped media_root (%s) — removed", exc)
        _cleanup_partial(actual_path)
        if thumbnail_absolute is not None:
            _cleanup_partial(thumbnail_absolute)
        return None

    record = _media_record(
        kind=kind,
        file_name=actual_path.name,
        mime_type=mime_type,
        size_bytes=actual_size,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        storage_path=storage_path,
        tg_file_id=tg_file_id,
        thumbnail_path=thumbnail_relative,
    )
    LOG.info("media saved chat=%s msg=%s kind=%s name=%s size=%s thumb=%s",
             chat_id, tg_message_id, kind, actual_path.name, actual_size,
             "yes" if thumbnail_relative else "no")
    return record


def _probe_image_size(path: Path, *, kind: str) -> tuple[Optional[int], Optional[int]]:
    """Read width/height from the decoded file (photos only, Pillow optional)."""
    if kind != MediaKind.PHOTO.value or not PILLOW_AVAILABLE:
        return None, None
    try:
        with Image.open(path) as opened:  # type: ignore[union-attr]
            return _positive_int(opened.width), _positive_int(opened.height)
    except Exception:
        LOG.debug("could not read image dimensions of %s", path.name, exc_info=True)
        return None, None


def _cleanup_partial(path: Any) -> None:
    """Remove a partially written file (never raises)."""
    try:
        candidate = Path(path)
        if candidate.is_file():
            candidate.unlink()
    except (OSError, TypeError, ValueError):
        LOG.debug("could not remove partial file %s", path, exc_info=True)


__all__ = [
    "DEFAULT_EXTENSION_BY_KIND",
    "MAX_DOWNLOAD_BYTES",
    "MAX_FILE_NAME_LENGTH",
    "MediaKind",
    "PILLOW_AVAILABLE",
    "THUMBNAIL_WIDTH",
    "chat_storage_dir",
    "detect_kind",
    "document_attribute",
    "download_media",
    "ensure_extension",
    "ensure_inside",
    "extract_dimensions",
    "extract_duration_seconds",
    "extract_size_bytes",
    "extract_tg_file_id",
    "guess_file_name",
    "guess_mime_type",
    "make_thumbnail",
    "message_storage_dir",
    "resolve_media_root",
    "resolve_storage_path",
    "sanitize_file_name",
    "to_posix_relative",
    "unique_path",
]
