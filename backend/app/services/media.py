"""Attachment storage for :class:`~app.models.MediaAttachment` (CONTRACTS_V2.md §2.1/§3.1).

One file is stored under ``{media_root}/chats/{chat_id}/{message_id}/`` and the
row only ever records the **relative** POSIX path, so the whole media tree can be
moved or mounted from a volume without touching the database:

===========================  ==================================================
relative ``storage_path``    ``chats/12/93/9f1c...0e.jpg``
relative ``thumbnail_path``  ``chats/12/93/9f1c...0e.thumb.jpg``
===========================  ==================================================

Design rules
------------
* **No path escapes.** :func:`resolve_media_path` resolves every relative path
  and refuses anything that lands outside ``media_root`` (``ValueError``), which
  also covers ``..`` tricks, absolute paths and Windows drive-relative names.
* **No orphans.** :func:`save_upload` writes the row *and* the file atomically
  from the caller's point of view: any failure after the bytes hit the disk
  removes them again, so a rejected/cancelled upload never leaves litter.
* **No hard Pillow dependency.** Thumbnails and pixel dimensions are a nicety:
  a broken/absent Pillow (or an undecodable file) degrades to
  ``thumbnail_path=None`` and keeps the original upload usable.
* **Streaming only.** Uploads are copied in 1 MiB chunks and never buffered in
  memory as a whole, and downloads go through Starlette's ``FileResponse``,
  which implements ``Range``/``ETag``/``Last-Modified`` for us.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
import uuid
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Final

from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import MediaAttachment, MessageKind
from app.schemas import AttachmentOut

logger = logging.getLogger("tgdesk.media")

__all__ = [
    "AUDIO_EXTENSIONS",
    "CHUNK_SIZE",
    "DOCUMENT_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MAX_FILES_PER_UPLOAD",
    "MEDIA_ROOT_SUBDIR",
    "THUMBNAIL_SUFFIX",
    "THUMBNAIL_WIDTH",
    "VIDEO_EXTENSIONS",
    "VOICE_EXTENSIONS",
    "attachment_path",
    "attachment_stream",
    "attachment_to_out",
    "delete_attachment_files",
    "detect_kind",
    "enforce_upload_request_size",
    "extension_of",
    "list_attachments",
    "media_root",
    "messages_media_root",
    "relative_storage_path",
    "resolve_media_path",
    "safe_file_name",
    "save_upload",
    "thumbnail_path_for",
]

#: Sub-directory of ``media_root`` holding every chat tree (CONTRACTS_V2.md §5).
MEDIA_ROOT_SUBDIR: Final[str] = "chats"

#: Copy buffer for incoming streams: big enough to amortise syscalls, small
#: enough that a 25 MiB upload never sits in memory.
CHUNK_SIZE: Final[int] = 1024 * 1024

#: How many files one ``POST /chats/{id}/attachments`` may carry. The router
#: validates it explicitly (``File``/``Form`` bodies are parsed before the handler
#: runs, so FastAPI cannot enforce it for us) and the pre-parse size guard below
#: multiplies ``max_upload_bytes`` by this number to bound the whole request.
MAX_FILES_PER_UPLOAD: Final[int] = 10

#: Longest file/extension fragment we agree to store (keeps within ``String(255)``).
MAX_FILE_NAME_LENGTH: Final[int] = 120
MAX_EXTENSION_LENGTH: Final[int] = 16

#: Generated preview geometry: fixed width, proportional height, JPEG.
THUMBNAIL_WIDTH: Final[int] = 320
THUMBNAIL_SUFFIX: Final[str] = ".thumb.jpg"
THUMBNAIL_MEDIA_TYPE: Final[str] = "image/jpeg"
THUMBNAIL_QUALITY: Final[int] = 82

#: Web-safe fallback when a row has no usable ``mime_type``.
DEFAULT_MEDIA_TYPE: Final[str] = "application/octet-stream"

#: Characters that would confuse a filesystem, a shell or a ``Content-Disposition``
#: header once the NUL/C0 controls have been stripped.
_UNSAFE_NAME_CHARS: Final[re.Pattern[str]] = re.compile(r'[<>:"|?*\x7f]+')
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]+")
_PATH_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[/\\]+")
_DOTS_AND_SPACES: Final[re.Pattern[str]] = re.compile(r"\.{2,}")
_EXTENSION_RE: Final[re.Pattern[str]] = re.compile(r"^\.?([A-Za-z0-9]{1,%d})$" % MAX_EXTENSION_LENGTH)
#: A Windows drive-absolute or drive-relative name (``C:/x``, ``C:x``, ``d:\x``).
_WINDOWS_DRIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")

#: Reserved DOS device names that are not ordinary file names on Windows.
_WINDOWS_RESERVED: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(10)}
    | {f"lpt{index}" for index in range(10)}
)

#: Extension -> kind fallback when ``content_type`` is missing or generic.
IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
)
VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".3gp", ".mpeg", ".mpg"}
)
VOICE_EXTENSIONS: Final[frozenset[str]] = frozenset({".ogg", ".oga", ".opus", ".weba"})
AUDIO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".flac", ".wma", ".aiff", ".aif", ".amr", ".mid", ".m4b"}
)
DOCUMENT_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
        ".rtf", ".txt", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".md", ".log",
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".epub", ".fb2",
    }
)

#: MIME prefixes for ``content_type``-driven detection.
_IMAGE_PREFIX: Final[str] = "image/"
_VIDEO_PREFIX: Final[str] = "video/"
_AUDIO_PREFIX: Final[str] = "audio/"

#: ``content_type`` values that carry no information (browsers send them when the
#: type is unknown), so the extension has the final word.
_GENERIC_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "application/unknown",
        "unknown/unknown",
        "application/x-unknown",
    }
)


# ---------------------------------------------------------------------------
# Roots and path resolution
# ---------------------------------------------------------------------------


def media_root() -> Path:
    """Absolute root of the media tree, created on demand.

    Read from ``settings`` on every call (never cached at import time) so tests
    that monkeypatch ``media_root`` get a clean tree without re-importing the
    module.
    """
    root = Path(settings.media_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def messages_media_root() -> Path:
    """``{media_root}/chats`` — parent of every ``{chat_id}/{message_id}`` folder."""
    root = media_root() / MEDIA_ROOT_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_media_path(storage_path: str) -> Path:
    """Resolve a stored *relative* path into an absolute path inside ``media_root``.

    Accepts the path exactly as stored (POSIX separators) and also tolerates the
    Windows form (``chats\\1\\2\\a.jpg``). Raises :class:`ValueError` for an empty
    path, for a path that does not stay inside ``media_root`` (``..`` segments,
    a symlink pointing out of the tree) and for a path that already exists as a
    symlink leaving the tree — the caller turns this into a ``404``.

    The containment check runs **after** ``resolve()``, so ``Path.is_relative_to``
    compares real paths: ``chats/../../etc/passwd`` becomes an absolute path
    outside the root and is rejected.
    """
    raw = "" if storage_path is None else str(storage_path).strip()
    if not raw or raw in {".", ".."}:
        raise ValueError("Пустой путь к файлу")

    normalised = raw.replace("\\", "/")
    # ``PurePath`` collapses ``//`` and ``./`` but *keeps* a UNC ``//server/share``
    # prefix, which would otherwise slip past the ``is_absolute()`` check below on
    # Windows (``\\server\share`` is absolute yet ``PurePosixPath`` never says so).
    if normalised.startswith("//"):
        raise ValueError(f"Сетевой путь запрещён: {storage_path!r}")
    pure = PurePosixPath(normalised)
    # An absolute path (``/etc/passwd``, ``C:/x`` or a drive-relative ``C:x``) can
    # never be a row of ours: rows are always relative to ``media_root``.
    if pure.is_absolute() or _WINDOWS_DRIVE_RE.match(normalised):
        raise ValueError(f"Абсолютный путь запрещён: {storage_path!r}")
    if any(part in {".", ".."} for part in pure.parts) or any(
        part.endswith(":") for part in pure.parts
    ):
        raise ValueError(f"Выход за пределы каталога медиа запрещён: {storage_path!r}")

    root = media_root().resolve()
    # ``Path.resolve()`` (non-strict) performs the ``..`` folding, so the check must
    # run on the *resolved* candidate — the raw segments above are only a fast reject.
    candidate = (root / pure).resolve()
    if not _is_within(candidate, root):
        raise ValueError(f"Выход за пределы каталога медиа запрещён: {storage_path!r}")
    return candidate


def _is_within(candidate: Path, root: Path) -> bool:
    """``True`` when ``candidate`` really lives inside ``root`` (or is ``root``).

    Replaces ``Path.is_relative_to`` on purpose: Python 3.12+ implements the
    realpath-free lexical variant, so a path that a symlink pivots outside the
    media tree would answer ``True``. Comparing the two strings requires an actual
    path-component prefix, which also rejects a sibling named ``media_evil``.
    """
    child = str(candidate)
    parent = str(root)
    if child == parent:
        return True
    if not parent.endswith(os.sep):
        parent = f"{parent}{os.sep}"
    return child.startswith(parent)


def attachment_path(attachment: MediaAttachment, *, variant: str | None = None) -> Path:
    """Absolute path of ``attachment`` (``variant="thumb"`` -> its preview).

    Raises :class:`ValueError` when the row has no such variant or when the
    stored path escapes the media tree.
    """
    storage_path = str(getattr(attachment, "storage_path", "") or "")
    if variant == "thumb":
        thumbnail = getattr(attachment, "thumbnail_path", None)
        if not thumbnail:
            raise ValueError("Миниатюра для этого файла не создавалась")
        return resolve_media_path(str(thumbnail))
    if variant not in (None, "", "original"):
        raise ValueError(f"Неизвестный вариант файла: {variant!r}")
    return resolve_media_path(storage_path)


def thumbnail_path_for(storage_path: str) -> str:
    """Relative preview path for ``storage_path`` (``a/b/c.jpg`` -> ``a/b/c.thumb.jpg``)."""
    normalised = str(storage_path).replace("\\", "/")
    pure = PurePosixPath(normalised)
    if not pure.name:
        return f"{normalised}{THUMBNAIL_SUFFIX}"
    return str(pure.with_name(f"{pure.stem}{THUMBNAIL_SUFFIX}"))


# ---------------------------------------------------------------------------
# File names and kinds
# ---------------------------------------------------------------------------


def extension_of(file_name: str | None) -> str:
    """Lower-case extension (with the dot) of ``file_name``, ``""`` when absent."""
    if not file_name:
        return ""
    suffix = PurePosixPath(str(file_name).replace("\\", "/")).suffix
    if not suffix:
        return ""
    cleaned = _EXTENSION_RE.match(suffix)
    return f".{cleaned.group(1).lower()}" if cleaned else ""


def safe_file_name(file_name: str | None, *, fallback: str = "file") -> str:
    """Sanitise an operator-supplied file name for storage and display.

    Strips path separators and ``..`` runs, removes control characters, folds
    Unicode to NFC, drops characters that break HTTP headers or Windows file
    systems, keeps the extension and truncates the stem so the whole name stays
    inside :data:`MAX_FILE_NAME_LENGTH` characters.

    >>> safe_file_name("../../etc/pa\\sswd.jpg")
    'etcpasswd.jpg'
    """
    raw = str(file_name or "").replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1]
    raw = unicodedata.normalize("NFC", raw)
    raw = _CONTROL_CHARS.sub("", raw)
    raw = _DOTS_AND_SPACES.sub(".", raw)
    raw = _PATH_SEPARATORS.sub("", raw)
    raw = _UNSAFE_NAME_CHARS.sub("", raw)
    raw = raw.strip().strip(".").strip()

    stem, dot, suffix = raw.rpartition(".")
    if not dot:
        stem, suffix = raw, ""
    suffix = suffix.strip().strip(".")[:MAX_EXTENSION_LENGTH]
    if not _EXTENSION_RE.match(suffix or ""):
        suffix = ""
    stem = stem.strip().strip(".").strip() or fallback

    budget = MAX_FILE_NAME_LENGTH - (len(suffix) + 1 if suffix else 0)
    if budget < 1:
        budget = MAX_FILE_NAME_LENGTH
        suffix = ""
    stem = stem[:budget].rstrip(". ") or fallback

    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"{stem}_"
    return f"{stem}.{suffix}" if suffix else stem


def detect_kind(content_type: str | None, file_name: str | None = None) -> str:
    """Map ``content_type`` (then the extension) onto a :class:`MessageKind` value.

    Order matters: a ``voice`` content type wins over the ``audio/`` prefix, and
    an ``image/*`` type wins over a misleading extension. Generic/absent types
    (``application/octet-stream``) fall through to the extension table.
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    extension = extension_of(file_name)

    if mime.startswith(_IMAGE_PREFIX):
        return MessageKind.PHOTO.value
    if mime.startswith(_VIDEO_PREFIX):
        return MessageKind.VIDEO.value
    if mime.startswith(_AUDIO_PREFIX) or mime == "application/ogg":
        # ``audio/ogg; codecs=opus`` is what Telegram uses for voice notes.
        if extension in VOICE_EXTENSIONS:
            return MessageKind.VOICE.value
        return MessageKind.AUDIO.value

    if mime not in _GENERIC_MEDIA_TYPES:
        # A concrete but unlisted type (``application/pdf``, ``text/csv``, ...).
        return MessageKind.DOCUMENT.value

    if extension in IMAGE_EXTENSIONS:
        return MessageKind.PHOTO.value
    if extension in VIDEO_EXTENSIONS:
        return MessageKind.VIDEO.value
    if extension in VOICE_EXTENSIONS:
        return MessageKind.VOICE.value
    if extension in AUDIO_EXTENSIONS:
        return MessageKind.AUDIO.value
    return MessageKind.DOCUMENT.value


def _media_type_of(content_type: str | None, file_name: str | None) -> str:
    """Best-effort MIME type: the declared one, else guessed from the name."""
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared not in _GENERIC_MEDIA_TYPES:
        return declared[:128]
    guessed = ""
    if file_name:
        try:
            import mimetypes

            guessed, _ = mimetypes.guess_type(str(file_name))
        except Exception:  # noqa: BLE001 - guessing is decorative
            guessed = ""
    return (guessed or DEFAULT_MEDIA_TYPE)[:128]


# ---------------------------------------------------------------------------
# Pillow helpers (optional dependency)
# ---------------------------------------------------------------------------


def _pillow() -> tuple[Any, Any, Any]:
    """Import ``(Image, ImageOps, UnidentifiedImageError)`` or raise ``ImportError``."""
    from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: PLC0415 - optional

    return Image, ImageOps, UnidentifiedImageError


def _draft_thumbnail(absolute: Path) -> tuple[str | None, int | None, int | None]:
    """Read the pixel size and write a JPEG preview next to the original.

    Returns ``(relative_thumbnail_path, width, height)``; anything unreadable or
    unsupported yields ``(None, None, None)`` instead of failing the upload.
    """
    try:
        Image, ImageOps, UnidentifiedImageError = _pillow()
    except Exception as exc:  # noqa: BLE001 - Pillow is optional at runtime
        logger.warning("Pillow недоступен, миниатюра не создана для %s: %s", absolute.name, exc)
        return None, None, None

    resized: Any = None
    try:
        with Image.open(absolute) as opened:
            opened.load()
            width, height = int(opened.width), int(opened.height)
            # ``exif_transpose`` keeps phone photos upright; ``thumbnail`` then
            # scales proportionally and never upscales a small original.
            upright = ImageOps.exif_transpose(opened) if hasattr(ImageOps, "exif_transpose") else opened
            resized = upright.convert("RGB")
            resampling = getattr(Image, "Resampling", None)
            resized.thumbnail(
                (THUMBNAIL_WIDTH, THUMBNAIL_WIDTH),
                resampling.LANCZOS if resampling is not None else 1,
            )
    except UnidentifiedImageError:
        logger.info("Файл %s не распознан как изображение — миниатюра пропущена", absolute.name)
        return None, None, None
    except Exception as exc:  # noqa: BLE001 - a broken image must not fail the upload
        logger.warning("Не удалось прочитать изображение %s: %s: %s", absolute.name, type(exc).__name__, exc)
        return None, None, None

    target = absolute.with_name(f"{absolute.stem}{THUMBNAIL_SUFFIX}")
    try:
        resized.save(target, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сохранить миниатюру %s: %s: %s", target.name, type(exc).__name__, exc)
        _remove_file(target)
        return None, width, height
    finally:
        try:
            resized.close()
        except Exception:  # noqa: BLE001 - closing a decoded image never matters
            pass

    return thumbnail_path_for(relative_storage_path(absolute)), width, height


def relative_storage_path(absolute: Path) -> str:
    """POSIX path of ``absolute`` relative to ``media_root`` (``chats/1/2/a.jpg``)."""
    root = media_root().resolve()
    resolved = Path(absolute).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Файл вне каталога медиа: {absolute}") from exc


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _remove_file(path: Path) -> None:
    """Delete ``path`` if it exists, swallowing (and logging) every error."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Не удалось удалить файл %s: %s: %s", path, type(exc).__name__, exc)


def _remove_files(paths: Iterable[Path]) -> None:
    """Delete several paths, each independently (best effort)."""
    for path in paths:
        _remove_file(path)


def _too_large(detail: str | None = None) -> HTTPException:
    """413 with the configured limit spelled out in MiB."""
    limit_mib = settings.max_upload_bytes / (1024 * 1024)
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=detail or f"Файл слишком большой: допустимо не более {limit_mib:g} МиБ",
    )


#: Extensions ``mimetypes`` serves inline and that execute script when a browser
#: renders them from our own origin. They are downgraded to a download.
_ACTIVE_CONTENT_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".html", ".htm", ".xhtml", ".xht", ".shtml", ".svg", ".svgz", ".xml", ".xsl", ".xslt", ".js", ".mjs"}
)

#: MIME prefixes a browser may render inside the page (``<img>``/``<video>``/``<audio>``).
_SAFE_INLINE_PREFIXES: Final[tuple[str, ...]] = ("image/", "audio/", "video/")

#: The only image type that can carry script — always served as a download.
_UNSAFE_INLINE_IMAGE_TYPES: Final[frozenset[str]] = frozenset({"image/svg+xml", "image/svg"})


async def enforce_upload_request_size(request: Request) -> int:
    """Reject an oversized upload **before** FastAPI parses the multipart body.

    ``save_upload`` already stops copying at ``settings.max_upload_bytes``, but by
    then Starlette has consumed the whole request: ``UploadFile`` spools to a
    temporary file once it exceeds 1 MiB, so a 5 GiB POST would first fill the
    system temp volume and only afterwards answer ``413``. This guard runs as a
    router dependency — i.e. before the body is read — and refuses on either

    * ``Content-Length`` above ``(MAX_FILES_PER_UPLOAD + 1) x max_upload_bytes``
      (the multipart framing needs a little headroom per part), or
    * a request that declares a body without a usable ``Content-Length`` (a
      chunked stream), because its real size cannot be bounded up front.

    Returns the declared length. Raises ``413`` / ``411`` / ``400``.
    """
    limit = int(settings.max_upload_bytes)
    budget = limit * (MAX_FILES_PER_UPLOAD + 1)

    raw_length = request.headers.get("content-length")
    if raw_length is None:
        transfer_encoding = request.headers.get("transfer-encoding", "")
        if transfer_encoding:
            logger.warning("upload without Content-Length (transfer-encoding=%s) refused", transfer_encoding)
            raise HTTPException(
                status_code=status.HTTP_411_LENGTH_REQUIRED,
                detail="Загрузка без Content-Length не поддерживается: отправьте файл с известным размером",
            )
        return 0
    try:
        declared = int(raw_length)
    except (TypeError, ValueError):
        declared = -1
    if declared < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный заголовок Content-Length",
        )
    if declared > budget:
        raise _too_large(
            "Слишком большой запрос: "
            f"допустимо не более {MAX_FILES_PER_UPLOAD} файлов по {limit / (1024 * 1024):g} МиБ"
        )
    return declared


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def save_upload(
    db: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    upload: UploadFile,
) -> MediaAttachment:
    """Stream ``upload`` to disk and persist its :class:`MediaAttachment` row.

    Steps (each one defensive):

    1. the message folder ``chats/{chat_id}/{message_id}`` is created;
    2. the stream is copied in :data:`CHUNK_SIZE` chunks, aborting with ``413``
       as soon as ``settings.max_upload_bytes`` is exceeded;
    3. the original name is sanitised, the on-disk name is a fresh
       ``{uuid4}#ext`` so two uploads of ``scan.pdf`` never collide;
    4. images get their pixel size and a ``{stem}.thumb.jpg`` preview;
    5. the row is committed; **any** failure deletes every file written so far,
       rolls the session back and re-raises (``HTTPException`` for ``413``/
       ``422``, otherwise ``500`` "Не удалось сохранить файл").

    The caller owns the surrounding message creation: the returned row is
    already committed and refreshed.
    """
    original_name = safe_file_name(getattr(upload, "filename", None))
    content_type = getattr(upload, "content_type", None)
    kind = detect_kind(content_type, original_name)
    mime_type = _media_type_of(content_type, original_name)
    extension = extension_of(original_name)

    folder = messages_media_root() / str(int(chat_id)) / str(int(message_id))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # No file was written yet, so the caller's session stays untouched: it
        # may still commit the (media-less) message.
        logger.exception("Не удалось создать каталог %s", folder)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось сохранить файл: каталог недоступен",
        ) from exc

    on_disk_name = f"{uuid.uuid4().hex}{extension}"
    absolute = folder / on_disk_name
    relative = f"{MEDIA_ROOT_SUBDIR}/{int(chat_id)}/{int(message_id)}/{on_disk_name}"
    thumbnail_absolute = absolute.with_name(f"{absolute.stem}{THUMBNAIL_SUFFIX}")

    written: list[Path] = [absolute]
    size_bytes = 0
    limit = int(settings.max_upload_bytes)

    try:
        with absolute.open("wb") as sink:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > limit:
                    raise _too_large()
                sink.write(chunk)
        if size_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Нельзя прикрепить пустой файл",
            )

        thumbnail_relative: str | None = None
        width: int | None = None
        height: int | None = None
        if kind == MessageKind.PHOTO.value:
            written.append(thumbnail_absolute)
            thumbnail_relative, width, height = _draft_thumbnail(absolute)

        attachment = MediaAttachment(
            message_id=int(message_id),
            kind=kind,
            file_name=original_name[:255],
            mime_type=mime_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            duration_seconds=None,
            storage_path=relative,
            tg_file_id=None,
            thumbnail_path=thumbnail_relative,
        )
        if thumbnail_relative is None and thumbnail_absolute.exists():
            # A preview file that the row does not point at is pure litter.
            thumbnail_absolute.unlink(missing_ok=True)

        db.add(attachment)
        await db.commit()
        await db.refresh(attachment)
    except HTTPException:
        await _rollback(db)
        _remove_files(written)
        _prune_folder(folder)
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a half-written upload behind
        logger.exception(
            "Не удалось сохранить вложение %r для сообщения %s: %s: %s",
            original_name,
            message_id,
            type(exc).__name__,
            exc,
        )
        await _rollback(db)
        _remove_files(written)
        _prune_folder(folder)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось сохранить файл",
        ) from exc
    finally:
        # The stream is owned by FastAPI, but closing it here releases the
        # spooled temporary file as soon as we are done reading.
        try:
            await upload.close()
        except Exception as exc:  # noqa: BLE001 - cosmetic
            logger.debug("upload.close() failed: %s", exc)

    logger.info(
        "вложение %s сохранено: message=%s kind=%s size=%s path=%s thumb=%s",
        attachment.id,
        message_id,
        kind,
        size_bytes,
        relative,
        attachment.thumbnail_path,
    )
    return attachment


async def _rollback(db: AsyncSession) -> None:
    """Roll back the caller's session without masking the original failure."""
    try:
        await db.rollback()
    except Exception as exc:  # noqa: BLE001
        logger.warning("rollback после неудачной загрузки не удался: %s: %s", type(exc).__name__, exc)


def _prune_folder(folder: Path) -> None:
    """Remove ``folder`` (and its now-empty parent) after a failed upload."""
    for candidate in (folder, folder.parent):
        try:
            candidate.rmdir()
        except OSError:
            return

async def delete_attachment_files(attachment: MediaAttachment) -> None:
    """Remove the stored file and its preview from disk.

    Never raises: a missing file is the normal case for a partial upload and a
    locked one is logged. Used by message deletion and by the retry path of
    :func:`save_upload`.
    """
    candidates: list[Path] = []
    for attribute in ("storage_path", "thumbnail_path"):
        relative = getattr(attachment, attribute, None)
        if not relative:
            continue
        try:
            candidates.append(resolve_media_path(str(relative)))
        except ValueError as exc:
            logger.warning("Отклонён небезопасный путь вложения %s: %s", attribute, exc)
    if not candidates:
        logger.debug("У вложения %s нет файлов на диске", getattr(attachment, "id", None))
        return

    for path in candidates:
        try:
            if path.is_dir():
                logger.warning("Пропущен каталог вместо файла: %s", path)
                continue
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Не удалось удалить файл вложения %s (%s): %s: %s",
                getattr(attachment, "id", None),
                path,
                type(exc).__name__,
                exc,
            )
    logger.info(
        "файлы вложения %s удалены (%d шт.)", getattr(attachment, "id", None), len(candidates)
    )


async def list_attachments(db: AsyncSession, message_id: int) -> list[MediaAttachment]:
    """All attachments of ``message_id``, oldest first (stable ``id`` order).

    ``selectinload`` pulls the owning message in a second query so a caller that
    later touches ``attachment.message`` never triggers a lazy load — the
    request session runs with ``autoflush=False`` and lazy IO there is a classic
    ``MissingGreenlet`` trap.
    """
    rows = (
        await db.execute(
            select(MediaAttachment)
            .where(MediaAttachment.message_id == int(message_id))
            .options(selectinload(MediaAttachment.message))
            .order_by(MediaAttachment.id.asc())
        )
    ).scalars().all()
    return list(rows)


def attachment_stream(attachment: MediaAttachment, *, variant: str | None = None) -> FileResponse:
    """Build the ``FileResponse`` for ``attachment`` (``variant="thumb"`` -> preview).

    Starlette's ``FileResponse`` implements ``Range`` (single and multipart),
    ``Accept-Ranges``, ``Content-Length``, ``ETag`` and ``Last-Modified`` itself,
    so nothing is re-implemented here — we only hand it the right path, media
    type and RFC 5987 encoded file name.

    Raises :class:`ValueError` when the row has no such variant, an unsafe path,
    or the file vanished from disk (the router maps it to ``404``).
    """
    path = attachment_path(attachment, variant=variant)
    if not path.is_file():
        raise ValueError(f"Файл вложения отсутствует на диске: {path.name}")

    want_thumb = variant == "thumb"
    original_name = str(getattr(attachment, "file_name", None) or path.name)
    if want_thumb:
        media_type = THUMBNAIL_MEDIA_TYPE
        stem = PurePosixPath(original_name).stem or "thumbnail"
        download_name = f"{stem}-thumb.jpg"
    else:
        media_type = str(getattr(attachment, "mime_type", None) or DEFAULT_MEDIA_TYPE)
        download_name = original_name
    # ``inline`` only for media a browser renders *inertly* (``<img>``/``<audio>``/
    # ``<video>``). Everything else — markup, SVG, scripts, archives — is served as
    # an attachment, so a stored ``.html`` can never execute on our own origin;
    # note that the row's ``mime_type`` is caller-declared and therefore untrusted.
    prefix = media_type.split("/", 1)[0].strip().lower()
    inline_ok = (
        prefix in {"image", "audio", "video"}
        and media_type.split(";", 1)[0].strip().lower() not in _UNSAFE_INLINE_IMAGE_TYPES
        and extension_of(download_name) not in _ACTIVE_CONTENT_EXTENSIONS
    )
    disposition = "inline" if inline_ok else "attachment"

    return FileResponse(
        path,
        media_type=media_type,
        filename=download_name,
        content_disposition_type=disposition,
    )


def attachment_to_out(attachment: MediaAttachment) -> AttachmentOut:
    """Serialise a row as the contract ``AttachmentOut`` (§3.1)."""
    return AttachmentOut.from_orm_attachment(attachment)
