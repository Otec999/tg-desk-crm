"""Unit tests for ``telethon_worker/media.py`` and the worker's pure helpers.

No network, no Redis and no real Telegram client is involved: every external
dependency is replaced by a small in-memory double. The suite is idempotent —
every test writes into a fresh ``tmp_path``.

Run from the repository root::

    .venv\\Scripts\\python.exe -m pytest telethon_worker/test_media.py -q
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

# ``media.py``/``worker.py`` live next to this file and are imported exactly as
# the worker imports them (``python worker.py`` from ``telethon_worker/``).
WORKER_DIR = Path(__file__).resolve().parent
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

# The backend's ``pyproject.toml`` enables the ``asyncio`` mode, but this file
# is run from the repository root, where no pytest config applies — enable it
# here so ``@pytest.mark.asyncio`` works in every environment, including CI
# invocations of this single file.
pytest_plugins = ("pytest_asyncio",)
DEFAULT_FIXTURE_LOOP_SCOPE = "function"


def pytest_configure(config: Any) -> None:
    """Enable pytest-asyncio's auto mode explicitly."""
    config.option.asyncio_mode = "auto"


import media  # noqa: E402  (path set up above)


# --------------------------------------------------------------------------- #
# Telethon types (installed dependency — imported for realistic doubles)
# --------------------------------------------------------------------------- #

from telethon.tl.types import (  # noqa: E402
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeImageSize,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessagePeerReaction,
    MessageReactions,
    PeerChannel,
    PeerUser,
    Photo,
    PhotoSize,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    UpdateMessageReactions,
)

#: Smallest valid JPEG (1x1 px) — used as the fake download payload.
#: Generated with Pillow at import time so the file decodes for real.
def _make_jpeg_bytes(width: int = 64, height: int = 32) -> bytes:
    """Return a real JPEG of ``width`` x ``height`` (needs Pillow)."""
    from PIL import Image  # imported here: Pillow is optional for media.py

    import io

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (70, 130, 180)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeDocument:
    """Minimal ``telethon.tl.types.Document`` stand-in."""

    def __init__(
        self,
        *,
        doc_id: int = 555,
        mime_type: str = "application/octet-stream",
        size: Optional[int] = 1024,
        attributes: Optional[list[Any]] = None,
    ) -> None:
        self.id = doc_id
        self.mime_type = mime_type
        self.size = size
        self.attributes = attributes or []
        self.access_hash = 987654321


class FakePhoto:
    """Minimal ``Photo`` stand-in with a couple of sizes."""

    def __init__(self, photo_id: int = 42, width: int = 800, height: int = 600) -> None:
        self.id = photo_id
        self.access_hash = 123123
        self.sizes = [
            PhotoSize(type="m", w=width, h=height, size=4096),
            PhotoSize(type="x", w=width, h=height, size=20480),
        ]


class FakeMessage:
    """Minimal ``telethon.tl.custom.Message`` stand-in (attribute driven)."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.pop("id", 93)
        self.media = kwargs.pop("media", None)
        self.photo = kwargs.pop("photo", None)
        self.document = kwargs.pop("document", None)
        self.voice = kwargs.pop("voice", None)
        self.video = kwargs.pop("video", None)
        self.video_note = kwargs.pop("video_note", None)
        self.audio = kwargs.pop("audio", None)
        self.sticker = kwargs.pop("sticker", None)
        self.gif = kwargs.pop("gif", None)
        self.text = kwargs.pop("text", None)
        self.message = kwargs.pop("message", self.text)
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeClient:
    """Stands in for ``TelegramClient.download_media``.

    Copies ``source_bytes`` into the requested path, mirroring Telethon's
    contract of returning the path (``str``) or ``None`` on failure.
    """

    def __init__(
        self,
        *,
        source_bytes: bytes = b"payload",
        result: str = "path",
        raise_exc: Optional[BaseException] = None,
        return_none: bool = False,
    ) -> None:
        self.source_bytes = source_bytes
        self.result = result
        self.raise_exc = raise_exc
        self.return_none = return_none
        self.calls: list[dict[str, Any]] = []

    async def download_media(self, message: Any, **kwargs: Any) -> Optional[str]:
        self.calls.append({"message": message, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.return_none:
            return None
        target = Path(kwargs["file"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.source_bytes)
        return str(target)

    async def send_file(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("send_file must not be called by media.download_media")


def photo_message(**kwargs: Any) -> FakeMessage:
    """A message carrying a photo (``media`` + ``photo``)."""
    fake_photo = kwargs.pop("photo", FakePhoto())
    return FakeMessage(media=MessageMediaPhoto(photo=fake_photo), photo=fake_photo, **kwargs)


def document_message(
    *,
    file_name: Optional[str] = "report.pdf",
    mime_type: str = "application/pdf",
    size: Optional[int] = 1024,
    extra_attributes: Optional[list[Any]] = None,
    **kwargs: Any,
) -> FakeMessage:
    """A message carrying a document with (optionally) a file-name attribute."""
    attributes: list[Any] = list(extra_attributes or [])
    if file_name is not None:
        attributes.append(DocumentAttributeFilename(file_name=file_name))
    document = FakeDocument(mime_type=mime_type, size=size, attributes=attributes)
    return FakeMessage(
        media=MessageMediaDocument(document=document),
        document=document,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# sanitize_file_name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "forbidden_check"),
    [
        ("../../../etc/passwd", "passwd"),
        ("..\\..\\Windows\\System32\\cmd.exe", "cmd.exe"),
        ("..", "file"),
        ("", "file"),
        ("   ", "file"),
        ("a/b/c.txt", "c.txt"),
        ("\x00\x01evil\x07name.dat", "evil"),
        ('report<with>illegal:"chars"|?.shot', "report_with_illegal"),
    ],
)
def test_sanitize_removes_traversal_and_separators(raw: str, forbidden_check: str) -> None:
    """No path traversal, no separators, no control characters survive."""
    result = media.sanitize_file_name(raw)
    assert ".." not in result
    assert "/" not in result
    assert "\\" not in result
    assert not any(ord(ch) < 32 for ch in result)
    assert not any(ch in result for ch in '<>:"|?*')
    assert forbidden_check[:4] in result


def test_sanitize_keeps_extension_and_limits_length() -> None:
    """The extension survives, the whole name fits in 120 characters."""
    long_stem = "отчёт-" * 60
    result = media.sanitize_file_name(f"{long_stem}.invoice.pdf")
    assert result.endswith(".pdf")
    assert len(result) <= media.MAX_FILE_NAME_LENGTH
    assert media.MAX_FILE_NAME_LENGTH == 120


def test_sanitize_strips_windows_reserved_names() -> None:
    """``CON.txt`` and friends cannot be created on Windows."""
    assert media.sanitize_file_name("CON.txt").startswith("_")
    assert media.sanitize_file_name("nul").startswith("_")
    assert media.sanitize_file_name("LPT1.log").startswith("_")


def test_sanitize_preserves_cyrillic_and_idempotent() -> None:
    """Russian names are legal file names; a second pass changes nothing."""
    once = media.sanitize_file_name("Платёжное поручение №5 (копия).pdf")
    twice = media.sanitize_file_name(once)
    assert "Платёжное поручение" in once
    assert once.endswith(".pdf")
    assert once == twice


def test_sanitize_does_not_mistake_version_for_extension() -> None:
    """``v1.2.3`` is not an extension (only the last dot can be one)."""
    result = media.sanitize_file_name("contract.v1.2.3")
    assert result == "contract.v1.2.3"


def test_ensure_extension_adds_suffix_from_mime() -> None:
    """A photo without a name still lands in ``*.jpg``."""
    assert media.ensure_extension("photo", kind="PHOTO", mime_type=None) == "photo.jpg"
    assert media.ensure_extension("scan", kind="DOCUMENT", mime_type="application/pdf").endswith(".pdf")
    assert media.ensure_extension("doc.txt", kind="DOCUMENT", mime_type=None) == "doc.txt"


# --------------------------------------------------------------------------- #
# detect_kind
# --------------------------------------------------------------------------- #


def test_detect_kind_photo() -> None:
    assert media.detect_kind(photo_message()) == "PHOTO"


def test_detect_kind_voice_and_sticker_and_animation() -> None:
    audio = FakeDocument(mime_type="audio/ogg")
    voice = FakeMessage(
        media=MessageMediaDocument(document=audio),
        document=audio,
        voice=DocumentAttributeAudio(duration=3, voice=True),
    )
    sticker_doc = FakeDocument(mime_type="image/webp")
    sticker = FakeMessage(
        media=MessageMediaDocument(document=sticker_doc),
        document=sticker_doc,
        sticker=DocumentAttributeSticker(alt="🙂", stickerset=types.SimpleNamespace(id=1, access_hash=1)),
    )
    animation_doc = FakeDocument(mime_type="video/mp4", attributes=[DocumentAttributeAnimated()])
    animation = FakeMessage(
        media=MessageMediaDocument(document=animation_doc),
        document=animation_doc,
        gif=DocumentAttributeAnimated(),
    )
    assert media.detect_kind(voice) == "VOICE"
    assert media.detect_kind(sticker) == "STICKER"
    assert media.detect_kind(animation) == "OTHER"


def test_detect_kind_video_and_audio_from_document() -> None:
    video_doc = FakeDocument(mime_type="video/mp4", attributes=[DocumentAttributeVideo(duration=12, w=640, h=480)])
    video_msg = FakeMessage(media=MessageMediaDocument(document=video_doc), document=video_doc)
    assert media.detect_kind(video_msg) == "VIDEO"

    audio_doc = FakeDocument(mime_type="audio/mpeg", attributes=[DocumentAttributeAudio(duration=180, voice=False)])
    audio_msg = FakeMessage(media=MessageMediaDocument(document=audio_doc), document=audio_doc)
    assert media.detect_kind(audio_msg) == "AUDIO"


def test_detect_kind_video_by_mime_without_attributes() -> None:
    video_doc = FakeDocument(mime_type="video/quicktime")
    video_msg = FakeMessage(media=MessageMediaDocument(document=video_doc), document=video_doc)
    assert media.detect_kind(video_msg) == "VIDEO"


def test_detect_kind_document_and_animation_by_extension() -> None:
    assert media.detect_kind(document_message(mime_type="application/pdf")) == "DOCUMENT"
    assert media.detect_kind(document_message(file_name="loop.gif", mime_type="")) == "OTHER"
    assert media.detect_kind(document_message(file_name="clip.mp4", mime_type="application/octet-stream")) == "VIDEO"


def test_detect_kind_image_document_is_photo() -> None:
    assert media.detect_kind(document_message(file_name="scan.png", mime_type="image/png")) == "PHOTO"


def test_detect_kind_text_when_no_media() -> None:
    assert media.detect_kind(FakeMessage()) == "TEXT"


# --------------------------------------------------------------------------- #
# Metadata extraction
# --------------------------------------------------------------------------- #


def test_extract_metadata_from_document() -> None:
    message = document_message(file_name="invoice.pdf", mime_type="application/pdf", size=4096)
    assert media.extract_size_bytes(message) == 4096
    assert media.guess_file_name(message, kind="DOCUMENT", mime_type="application/pdf") == "invoice.pdf"
    assert media.guess_mime_type(message, kind="DOCUMENT", file_name="invoice.pdf") == "application/pdf"
    assert media.extract_tg_file_id(message) == "document/555/987654321"


def test_extract_dimensions_and_duration() -> None:
    video_doc = FakeDocument(
        mime_type="video/mp4",
        attributes=[DocumentAttributeVideo(duration=90.4, w=1280, h=720)],
    )
    message = FakeMessage(media=MessageMediaDocument(document=video_doc), document=video_doc)
    assert media.extract_dimensions(message) == (1280, 720)
    assert media.extract_duration_seconds(message) == 90


def test_extract_dimensions_from_photo_sizes() -> None:
    assert media.extract_dimensions(photo_message(photo=FakePhoto(width=800, height=600))) == (800, 600)


def test_extract_tg_file_id_for_photo() -> None:
    message_id = media.extract_tg_file_id(photo_message(photo=FakePhoto(photo_id=7)))
    assert message_id == "photo/7/123123"


def test_tg_file_id_format_and_length_bound() -> None:
    """``{type}/{id}/{access_hash}`` only — no hex file_reference.

    The ``media_attachments.tg_file_id`` column is ``VARCHAR(255)``: a hex
    ``file_reference`` can overflow it and blow up the backend insert. The
    worst realistic id (20-digit ids, large hashes) stays far below the limit.
    """
    huge_doc = FakeDocument(doc_id=2**62, mime_type="application/pdf")
    huge_doc.access_hash = 2**61
    huge_doc.file_reference = b"\xab" * 64  # would add 128 chars if included
    message = FakeMessage(
        media=MessageMediaDocument(document=huge_doc), document=huge_doc
    )

    file_id = media.extract_tg_file_id(message)

    assert file_id is not None
    assert file_id.count("/") == 2
    assert file_id.split("/")[0] == "document"
    assert all(part.isdigit() for part in file_id.split("/")[1:])
    assert len(file_id) <= 255
    assert "abab" not in file_id.lower()


# --------------------------------------------------------------------------- #
# download_media (mocked Telethon client)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_download_media_saves_photo_and_thumbnail(tmp_path: Path) -> None:
    """Photo lands in chats/{chat}/{msg}/, thumbnail is a 320 px JPEG."""
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=_make_jpeg_bytes(640, 480))
    message = photo_message(id=93)

    record = await media.download_media(client, message, media_root, -1001234567890)

    assert record is not None
    assert record["kind"] == "PHOTO"
    assert record["storage_path"] == "chats/-1001234567890/93/photo_42.jpg"
    assert record["file_name"] == "photo_42.jpg"
    assert record["mime_type"] == "image/jpeg"
    assert record["size_bytes"] > 0
    assert record["width"] == 800 and record["height"] == 600  # from Telegram sizes
    assert record["tg_file_id"].startswith("photo/42/")
    assert record["duration_seconds"] is None

    absolute = media.resolve_storage_path(record["storage_path"], media_root)
    assert absolute is not None and absolute.is_file()

    if media.PILLOW_AVAILABLE:
        assert record["thumbnail_path"] == "chats/-1001234567890/93/photo_42.jpg.thumb.jpg"
        thumb = media.resolve_storage_path(record["thumbnail_path"], media_root)
        assert thumb is not None and thumb.is_file()
        from PIL import Image

        with Image.open(thumb) as image:
            assert image.width == media.THUMBNAIL_WIDTH
            assert image.size[0] == 320
    else:  # pragma: no cover - Pillow is a declared dependency
        assert record["thumbnail_path"] is None


@pytest.mark.asyncio
async def test_download_media_document_has_no_thumbnail(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"%PDF-1.4 test")
    record = await media.download_media(client, document_message(file_name="счёт.pdf"), media_root, 7)

    assert record is not None
    assert record["kind"] == "DOCUMENT"
    assert record["storage_path"] == "chats/7/93/счёт.pdf"
    assert record["thumbnail_path"] is None
    assert "\\" not in record["storage_path"]  # POSIX style on Windows too


@pytest.mark.asyncio
async def test_download_media_rejects_oversized_without_downloading(tmp_path: Path) -> None:
    """A file above the ceiling is reported but never fetched."""
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"x" * 10)
    message = document_message(file_name="huge.zip", size=80 * 1024 * 1024)

    record = await media.download_media(client, message, media_root, 7, max_download_bytes=1024)

    assert record is not None
    assert record["storage_path"] is None
    assert record["kind"] == "DOCUMENT"
    assert record["size_bytes"] == 80 * 1024 * 1024
    assert client.calls == []  # nothing was downloaded
    assert not (media_root / "chats" / "7").exists()


@pytest.mark.asyncio
async def test_download_media_default_limit_is_50_mib() -> None:
    assert media.MAX_DOWNLOAD_BYTES == 50 * 1024 * 1024


@pytest.mark.asyncio
async def test_download_media_returns_none_for_text_message(tmp_path: Path) -> None:
    client = FakeClient()
    assert await media.download_media(client, FakeMessage(), tmp_path / "media", 7) is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_download_media_swallows_download_errors(tmp_path: Path) -> None:
    """A failing Telethon download must not propagate into the worker loop."""
    client = FakeClient(raise_exc=RuntimeError("boom"))
    record = await media.download_media(client, photo_message(), tmp_path / "media", 7)
    assert record is None


@pytest.mark.asyncio
async def test_download_media_returns_none_when_telegram_returns_nothing(tmp_path: Path) -> None:
    client = FakeClient(return_none=True)
    assert await media.download_media(client, photo_message(), tmp_path / "media", 7) is None


@pytest.mark.asyncio
async def test_download_media_sanitises_dangerous_names(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"data")
    message = document_message(file_name="../../evil\\payload?.txt", mime_type="text/plain")

    record = await media.download_media(client, message, media_root, 7)

    assert record is not None
    assert record["storage_path"] is not None
    assert ".." not in record["storage_path"]
    assert "\\" not in record["storage_path"]
    resolved = media.resolve_storage_path(record["storage_path"], media_root)
    assert resolved is not None
    assert resolved.resolve().is_relative_to(media_root.resolve())


@pytest.mark.asyncio
async def test_download_media_never_overwrites(tmp_path: Path) -> None:
    """Two attachments with one name in one message folder both survive."""
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"first")
    message = document_message(file_name="IMG_0001.jpg", mime_type="image/jpeg", size=5)
    first = await media.download_media(client, message, media_root, 7)

    client_second = FakeClient(source_bytes=b"second")
    second = await media.download_media(client_second, message, media_root, 7)

    assert first is not None and second is not None
    assert first["storage_path"] != second["storage_path"]
    assert second["storage_path"].endswith("IMG_0001-1.jpg")


@pytest.mark.asyncio
async def test_download_media_photo_without_pillow_logs_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pillow missing = no thumbnail, but the record is still returned."""
    monkeypatch.setattr(media, "PILLOW_AVAILABLE", False)
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"not a real image")

    record = await media.download_media(client, photo_message(), media_root, 7)

    assert record is not None
    assert record["storage_path"] == "chats/7/93/photo_42.jpg"
    assert record["thumbnail_path"] is None
    # Dimension probe is unavailable without Pillow; Telegram's sizes win.
    assert record["width"] == 800 and record["height"] == 600


@pytest.mark.asyncio
async def test_download_media_thumbnail_failure_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt image must yield a saved file with ``thumbnail_path=None``."""
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"definitely not a jpeg")

    record = await media.download_media(client, photo_message(), media_root, 7)

    assert record is not None
    assert record["storage_path"] is not None
    assert record["thumbnail_path"] is None


@pytest.mark.asyncio
async def test_download_media_enforces_limit_after_download(tmp_path: Path) -> None:
    """When Telegram reports no size, the limit is applied to the real file."""
    media_root = tmp_path / "media"
    client = FakeClient(source_bytes=b"x" * 2048)
    message = document_message(file_name="big.bin", size=None)

    record = await media.download_media(client, message, media_root, 7, max_download_bytes=1024)

    assert record is not None
    assert record["storage_path"] is None
    assert record["size_bytes"] == 2048
    assert not (media_root / "chats" / "7" / "93" / "big.bin").exists()


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def test_to_posix_relative_and_resolve_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "media"
    target = root / "chats" / "-100123" / "5" / "a.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    relative = media.to_posix_relative(target, root)
    assert relative == "chats/-100123/5/a.jpg"
    assert media.resolve_storage_path(relative, root) == target


def test_to_posix_relative_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    with pytest.raises(ValueError):
        media.to_posix_relative(outside, root)


def test_resolve_storage_path_rejects_traversal(tmp_path: Path) -> None:
    assert media.resolve_storage_path("../etc/passwd", tmp_path) is None
    assert media.resolve_storage_path("/abs/path.jpg", tmp_path) is None
    assert media.resolve_storage_path(None, tmp_path) is None


def test_ensure_inside_blocks_escape(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(ValueError):
        media.ensure_inside(tmp_path / "outside.txt", root)


# --------------------------------------------------------------------------- #
# Worker helpers (imported lazily: worker.py requires the full runtime stack)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def worker_module() -> Any:
    """Import ``worker.py`` once (it pulls Telethon + redis + dotenv)."""
    import worker  # noqa: PLC0415  - deliberate lazy import

    return worker


def test_worker_trim_caption_limits_to_1024(worker_module: Any) -> None:
    trimmed = worker_module.trim_caption("я" * 5000)
    assert trimmed is not None
    assert len(trimmed) == worker_module.TG_MAX_CAPTION_LENGTH == 1024
    assert trimmed.endswith("…")
    assert worker_module.trim_caption("  ") is None
    assert worker_module.trim_caption("ok") == "ok"


def test_worker_normalize_reaction(worker_module: Any) -> None:
    assert worker_module.normalize_reaction_emoji("👍") == "👍"
    assert worker_module.normalize_reaction_emoji("📉") is None
    assert worker_module.normalize_reaction_emoji(None) is None


def _peer_reaction(user_id: int, emoticon: str) -> MessagePeerReaction:
    """Build a ``MessagePeerReaction`` (``date`` is a required field)."""
    from datetime import datetime, timezone

    return MessagePeerReaction(
        peer_id=PeerUser(user_id=user_id),
        reaction=ReactionEmoji(emoticon=emoticon),
        date=datetime.now(timezone.utc),
    )


def test_worker_extract_reactions_filters_unknown_emoji(worker_module: Any) -> None:
    """Only the six contract emoji survive; custom emoji are dropped."""
    payload = MessageReactions(
        results=[
            ReactionCount(reaction=ReactionEmoji(emoticon="👍"), count=2),
            ReactionCount(reaction=ReactionEmoji(emoticon="🎉"), count=9),
            ReactionCount(reaction=ReactionCustomEmoji(document_id=123456), count=4),
            ReactionCount(reaction=ReactionEmoji(emoticon="🔥"), count=1),
        ],
        recent_reactions=[_peer_reaction(777, "👍"), _peer_reaction(778, "🔥")],
    )
    result = worker_module.extract_reactions(payload)
    assert result == [
        {"emoji": "👍", "count": 2, "user_ids": [777]},
        {"emoji": "🔥", "count": 1, "user_ids": [778]},
    ]


def test_worker_extract_reactions_empty_payload(worker_module: Any) -> None:
    assert worker_module.extract_reactions(None) == []
    assert worker_module.extract_reactions(MessageReactions(results=[])) == []


def test_worker_peer_chat_id_marks_channel(worker_module: Any) -> None:
    assert worker_module._peer_chat_id(PeerChannel(channel_id=4338873193)) == -1004338873193


def test_worker_channels_and_kinds(worker_module: Any) -> None:
    assert worker_module.CHANNEL_REACTIONS == "tg_reactions"
    assert worker_module.KIND_REACTION_UPDATE == "REACTION_UPDATE"
    assert worker_module.CHANNEL_INCOMING == "tg_incoming"
    assert worker_module.CHANNEL_OUTGOING_ACK == "tg_outgoing_ack"


def test_worker_message_kind_delegates_to_media(worker_module: Any) -> None:
    assert worker_module.message_kind(photo_message()) == "PHOTO"
    assert worker_module.message_kind(FakeMessage()) == "TEXT"


def test_worker_attachment_placeholder(worker_module: Any) -> None:
    assert worker_module._attachment_placeholder([]) == ""
    assert worker_module._attachment_placeholder([{"kind": "PHOTO"}]).startswith("[photo]")
    label = worker_module._attachment_placeholder([{"kind": "DOCUMENT", "file_name": "a.pdf"}])
    assert "a.pdf" in label


# --------------------------------------------------------------------------- #
# Reaction update plumbing (worker instance, no network)
# --------------------------------------------------------------------------- #


class FakeBus:
    """Collects published payloads instead of talking to Redis."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.stats = types.SimpleNamespace(
            incoming=0, deleted=0, edited=0, acks=0, failures=0, reactions=0,
            media_downloaded=0, media_failed=0, attachments_sent=0,
        )

    async def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        self.published.append((channel, payload))
        return True


def _make_worker(cfg: Any) -> Any:
    import worker  # noqa: PLC0415

    instance = worker.TelethonWorker(cfg)
    instance.bus = FakeBus()
    return instance


@pytest.fixture()
def worker_config(tmp_path: Path) -> Any:
    import config as worker_config_module  # noqa: PLC0415

    return worker_config_module.WorkerConfig(
        api_id=1,
        api_hash="x" * 32,
        media_root=str(tmp_path / "media"),
        max_download_bytes=1024 * 1024,
    )


@pytest.mark.asyncio
async def test_raw_reaction_update_publishes_envelope(worker_config: Any) -> None:
    """``UpdateMessageReactions`` -> ``tg_reactions`` with the contract shape."""
    instance = _make_worker(worker_config)
    update = UpdateMessageReactions(
        peer=PeerChannel(channel_id=4338873193),
        msg_id=93,
        reactions=MessageReactions(
            results=[ReactionCount(reaction=ReactionEmoji(emoticon="👍"), count=2)],
            recent_reactions=[_peer_reaction(123, "👍")],
        ),
    )

    await instance._on_raw_update(update)

    assert len(instance.bus.published) == 1
    channel, payload = instance.bus.published[0]
    assert channel == "tg_reactions"
    assert payload == {
        "kind": "REACTION_UPDATE",
        "tg_chat_id": -1004338873193,
        "tg_msg_id": 93,
        "reactions": [{"emoji": "👍", "count": 2, "user_ids": [123]}],
    }


@pytest.mark.asyncio
async def test_duplicate_reaction_update_is_not_republished(worker_config: Any) -> None:
    """Both handlers firing for one update must not double-count."""
    instance = _make_worker(worker_config)
    update = UpdateMessageReactions(
        peer=PeerChannel(channel_id=1),
        msg_id=5,
        reactions=MessageReactions(
            results=[ReactionCount(reaction=ReactionEmoji(emoticon="❤️"), count=1)]
        ),
    )
    await instance._on_raw_update(update)
    await instance._on_raw_update(update)
    assert len(instance.bus.published) == 1


@pytest.mark.asyncio
async def test_cleared_reactions_publish_empty_list(worker_config: Any) -> None:
    """All reactions removed -> ``reactions: []`` (backend deletes its rows)."""
    instance = _make_worker(worker_config)
    payload = MessageReactions(results=[])
    await instance._publish_reaction_update(-1001234, 77, payload)
    channel, envelope = instance.bus.published[0]
    assert channel == "tg_reactions"
    assert envelope["reactions"] == []


@pytest.mark.asyncio
async def test_reaction_update_respects_monitored_chats(worker_config: Any) -> None:
    import dataclasses  # noqa: PLC0415

    cfg = dataclasses.replace(worker_config, monitored_chat_ids=[-100999])
    instance = _make_worker(cfg)
    await instance._publish_reaction_update(-1001234, 77, MessageReactions(results=[]))
    assert instance.bus.published == []


# --------------------------------------------------------------------------- #
# Incoming message with media -> tg_incoming payload
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_incoming_photo_payload_contains_media_caption_kind(
    worker_config: Any, tmp_path: Path
) -> None:
    """A photo with a caption produces ``media``/``caption``/``message_kind``."""
    instance = _make_worker(worker_config)
    instance.client = FakeClient(source_bytes=_make_jpeg_bytes(64, 64))
    instance._connected = True

    message = photo_message(id=501, text="Вот чек", message="Вот чек")

    class FakeEvent:
        chat_id = -1001234567890
        message = None
        is_private = False
        is_channel = True
        is_group = True
        chat = types.SimpleNamespace(title="Merchant Alpha — Payouts", username=None)
        raw_text = "Вот чек"
        reply_to_msg_id = None

        async def get_sender(self) -> Any:
            return types.SimpleNamespace(
                first_name="Иван", last_name="Петров", username="ivan",
                bot=False, id=555,
            )

    event = FakeEvent()
    event.message = message

    await instance._handle_new_message(event)

    channel, payload = instance.bus.published[0]
    assert channel == "tg_incoming"
    assert payload["kind"] == "NEW_MESSAGE"
    assert payload["message_kind"] == "PHOTO"
    assert payload["caption"] == "Вот чек"
    assert payload["sender_type"] == "USER"
    assert payload["merchant_code"] == "MRC-ALPHA"
    assert len(payload["media"]) == 1
    attachment = payload["media"][0]
    assert set(attachment) == {
        "kind", "file_name", "mime_type", "size_bytes", "width", "height",
        "duration_seconds", "storage_path", "tg_file_id", "thumbnail_path",
    }
    assert attachment["storage_path"] == "chats/-1001234567890/501/photo_42.jpg"


@pytest.mark.asyncio
async def test_incoming_bot_message_keeps_sender_type_bot(worker_config: Any) -> None:
    instance = _make_worker(worker_config)

    class FakeEvent:
        chat_id = -1001234567890
        is_private = False
        is_channel = True
        is_group = True
        chat = types.SimpleNamespace(title="Support", username=None)
        raw_text = "Бот на связи"
        reply_to_msg_id = None
        message = FakeMessage(id=900, text="Бот на связи", message="Бот на связи")

        async def get_sender(self) -> Any:
            return types.SimpleNamespace(first_name="Support", last_name="",
                                         username="support_bot", bot=True, id=42)

    await instance._handle_new_message(FakeEvent())

    _channel, payload = instance.bus.published[0]
    assert payload["sender_type"] == "BOT"
    assert payload["is_bot"] is True
    assert payload["media"] == []
    assert payload["caption"] is None
    assert payload["message_kind"] == "TEXT"


@pytest.mark.asyncio
async def test_incoming_text_message_has_empty_media_list(worker_config: Any) -> None:
    instance = _make_worker(worker_config)

    class FakeEvent:
        chat_id = -1001234567890
        is_private = False
        is_channel = True
        is_group = True
        chat = types.SimpleNamespace(title="Group", username=None)
        raw_text = "привет"
        reply_to_msg_id = None
        message = FakeMessage(id=901, text="привет", message="привет")

        async def get_sender(self) -> Any:
            return types.SimpleNamespace(first_name="A", last_name="", username=None, bot=False, id=1)

    await instance._handle_new_message(FakeEvent())
    _channel, payload = instance.bus.published[0]
    assert payload["media"] == []
    assert payload["caption"] is None


# --------------------------------------------------------------------------- #
# Outgoing attachment
# --------------------------------------------------------------------------- #


class SendRecordingClient:
    """Records ``send_message``/``send_file`` calls; ``get_entity`` is a no-op."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_files: list[dict[str, Any]] = []

    async def get_entity(self, chat_id: int) -> Any:
        return chat_id

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent_messages.append(kwargs)
        return types.SimpleNamespace(id=1001)

    async def send_file(self, **kwargs: Any) -> Any:
        self.sent_files.append(kwargs)
        return types.SimpleNamespace(id=1002)


def _outgoing_envelope(command: dict[str, Any]) -> dict[str, Any]:
    import json as json_module  # noqa: PLC0415

    return {"type": "message", "data": json_module.dumps(command)}


@pytest.mark.asyncio
async def test_outgoing_with_attachment_uses_send_file(
    worker_config: Any, tmp_path: Path
) -> None:
    instance = _make_worker(worker_config)
    client = SendRecordingClient()
    instance.client = client
    instance._connected = True
    attachment = tmp_path / "отчёт.pdf"
    attachment.write_bytes(b"%PDF-1.4 content")

    await instance._handle_outgoing_message(
        _outgoing_envelope(
            {
                "cmd_id": "cmd-1",
                "chat_id": 7,
                "tg_chat_id": -1001234567890,
                "message_id": 55,
                "text": "Держите отчёт",
                "attachment_path": str(attachment),
                "reply_to_tg_msg_id": 44,
            }
        )
    )

    assert client.sent_messages == []
    assert len(client.sent_files) == 1
    sent = client.sent_files[0]
    assert sent["file"] == str(attachment)
    assert sent["caption"] == "Держите отчёт"
    assert sent["reply_to"] == 44
    assert sent["parse_mode"] is None

    channel, ack = instance.bus.published[-1]
    assert channel == "tg_outgoing_ack"
    assert ack["ok"] is True
    assert ack["tg_msg_id"] == 1002
    assert ack["attachment_sent"] is True


@pytest.mark.asyncio
async def test_outgoing_text_without_attachment_is_unchanged(worker_config: Any) -> None:
    """Backward compatibility: no ``attachment_path`` -> ``send_message``."""
    instance = _make_worker(worker_config)
    client = SendRecordingClient()
    instance.client = client
    instance._connected = True

    await instance._handle_outgoing_message(
        _outgoing_envelope(
            {
                "cmd_id": "cmd-2",
                "chat_id": 7,
                "tg_chat_id": -1001234567890,
                "message_id": 56,
                "text": "Просто текст",
            }
        )
    )

    assert len(client.sent_messages) == 1
    assert client.sent_files == []
    _channel, ack = instance.bus.published[-1]
    assert ack["ok"] is True
    assert ack["tg_msg_id"] == 1001
    assert "attachment_sent" not in ack  # legacy ACK shape preserved


@pytest.mark.asyncio
async def test_outgoing_missing_attachment_is_negative_ack(
    worker_config: Any, tmp_path: Path
) -> None:
    instance = _make_worker(worker_config)
    client = SendRecordingClient()
    instance.client = client
    instance._connected = True

    await instance._handle_outgoing_message(
        _outgoing_envelope(
            {
                "cmd_id": "cmd-3",
                "chat_id": 7,
                "tg_chat_id": -1001234567890,
                "message_id": 57,
                "text": "",
                "attachment_path": str(tmp_path / "nope.bin"),
            }
        )
    )

    assert client.sent_files == [] and client.sent_messages == []
    _channel, ack = instance.bus.published[-1]
    assert ack["ok"] is False
    assert "does not exist" in (ack["error"] or "")


@pytest.mark.asyncio
async def test_outgoing_attachment_send_error_is_negative_ack(
    worker_config: Any, tmp_path: Path
) -> None:
    class ExplodingClient(SendRecordingClient):
        async def send_file(self, **kwargs: Any) -> Any:
            raise RuntimeError("upload exploded")

    instance = _make_worker(worker_config)
    instance.client = ExplodingClient()
    instance._connected = True
    attachment = tmp_path / "a.txt"
    attachment.write_text("x", encoding="utf-8")

    await instance._handle_outgoing_message(
        _outgoing_envelope(
            {
                "cmd_id": "cmd-4",
                "chat_id": 7,
                "tg_chat_id": -1001234567890,
                "message_id": 58,
                "text": "",
                "attachment_path": str(attachment),
            }
        )
    )

    _channel, ack = instance.bus.published[-1]
    assert ack["ok"] is False
    assert "RuntimeError" in (ack["error"] or "")


@pytest.mark.asyncio
async def test_outgoing_long_caption_for_file_is_rejected(
    worker_config: Any, tmp_path: Path
) -> None:
    instance = _make_worker(worker_config)
    client = SendRecordingClient()
    instance.client = client
    instance._connected = True
    attachment = tmp_path / "a.txt"
    attachment.write_text("x", encoding="utf-8")

    await instance._handle_outgoing_message(
        _outgoing_envelope(
            {
                "cmd_id": "cmd-5",
                "chat_id": 7,
                "tg_chat_id": -1001234567890,
                "message_id": 59,
                "text": "т" * 2000,
                "attachment_path": str(attachment),
            }
        )
    )

    assert client.sent_files == []
    _channel, ack = instance.bus.published[-1]
    assert ack["ok"] is False
    assert "caption too long" in (ack["error"] or "")


# --------------------------------------------------------------------------- #
# Config (media_root / max_download_bytes)
# --------------------------------------------------------------------------- #


def test_config_media_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults follow the contract: ``storage/media`` and 50 MiB."""
    import config as worker_config_module  # noqa: PLC0415

    monkeypatch.delenv("MEDIA_ROOT", raising=False)
    monkeypatch.delenv("TG_MEDIA_ROOT", raising=False)
    monkeypatch.delenv("MAX_DOWNLOAD_BYTES", raising=False)
    monkeypatch.delenv("MEDIA_MAX_DOWNLOAD_BYTES", raising=False)
    monkeypatch.delenv("TG_MAX_DOWNLOAD_BYTES", raising=False)

    cfg = worker_config_module.load_config(validate_credentials=False)

    assert cfg.media_root == "storage/media"
    assert cfg.media_root_path == (worker_config_module.REPO_ROOT / "storage" / "media").resolve()
    assert cfg.max_download_bytes == 50 * 1024 * 1024


def test_config_media_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import config as worker_config_module  # noqa: PLC0415

    monkeypatch.setenv("MEDIA_ROOT", "D:/crm/media")
    monkeypatch.setenv("MAX_DOWNLOAD_BYTES", "25MB")
    cfg = worker_config_module.load_config(validate_credentials=False)

    assert cfg.media_root == "D:/crm/media"
    assert cfg.max_download_bytes == 25 * 1024 * 1024


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1024", 1024),
        ("50MB", 50 * 1024 * 1024),
        ("50 mb", 50 * 1024 * 1024),
        ("1.5 GiB", int(1.5 * 1024**3)),
        ("800k", 800 * 1024),
    ],
)
def test_parse_size_bytes(raw: str, expected: int) -> None:
    import config as worker_config_module  # noqa: PLC0415

    assert worker_config_module.parse_size_bytes(raw) == expected


def test_parse_size_bytes_rejects_garbage() -> None:
    import config as worker_config_module  # noqa: PLC0415

    with pytest.raises(ValueError):
        worker_config_module.parse_size_bytes("50 bananas")


def test_thumbnail_width_is_320() -> None:
    assert media.THUMBNAIL_WIDTH == 320


def test_media_kind_mirrors_backend_enum() -> None:
    """The worker's copy must stay byte-identical with CONTRACTS_V2 §1."""
    assert {member.value for member in media.MediaKind} == {
        "TEXT", "PHOTO", "DOCUMENT", "VOICE", "VIDEO", "STICKER", "AUDIO", "OTHER",
    }
    assert math.isclose(media.MAX_DOWNLOAD_BYTES, 52428800)
