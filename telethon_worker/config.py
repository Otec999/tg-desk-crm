"""Configuration loading for the Telethon <-> Redis bridge worker.

The worker is launched **from** ``telethon_worker/``::

    python worker.py

but every path/env lookup is anchored to the **repository root** so the
worker can also be launched from any other directory (NSSM, Task Scheduler,
systemd ``WorkingDirectory=`` …).

Environment precedence (highest wins):

1. real process environment variables,
2. ``<repo root>/.env``        (the canonical file shared with the backend),
3. ``./.env``                  (cwd of the process, convenient for NSSM users).

This module contains **no logging**: it returns data, ``worker.py`` logs it.
That keeps secrets out of any premature ``logging.info`` call and makes the
pure helpers (parsing, masking) trivially unit-testable.

CONTRACTS.md §1 is the single source of truth for variable names.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is a hard dependency (requirements.txt), but the worker
    # must still be importable for `--help` on a half-installed machine.
    from dotenv import load_dotenv

    _DOTENV_AVAILABLE = True
except Exception:  # pragma: no cover - defensive, dependencies are pinned
    load_dotenv = None  # type: ignore[assignment]
    _DOTENV_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: ``telethon_worker/config.py`` -> parents[0] = ``telethon_worker``,
#: parents[1] = repository root (where ``.env`` / ``.env.example`` live).
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: Default session file, relative to the repository root (CONTRACTS §1).
DEFAULT_SESSION_FILE: str = "telethon_worker/tg_desk.session"

#: Default media store, relative to the repository root (CONTRACTS_V2 §7).
#: Must match ``backend.app.config.Settings.media_root`` because
#: ``media_attachments.storage_path`` is stored relative to this directory.
DEFAULT_MEDIA_ROOT: str = "storage/media"

#: Default ceiling for one downloaded attachment (CONTRACTS_V2 §5): 50 MiB.
DEFAULT_MAX_DOWNLOAD_BYTES: int = 50 * 1024 * 1024

#: Env keys for ``media_root`` (first non-empty wins).
MEDIA_ROOT_ENV_KEYS: tuple[str, ...] = ("MEDIA_ROOT", "TG_MEDIA_ROOT")

#: Env keys for the download ceiling (first non-empty wins).
MAX_DOWNLOAD_ENV_KEYS: tuple[str, ...] = (
    "MAX_DOWNLOAD_BYTES",
    "MEDIA_MAX_DOWNLOAD_BYTES",
    "TG_MAX_DOWNLOAD_BYTES",
)

#: Lower bound for ``max_download_bytes``: anything smaller cannot hold even a
#: single Telegram thumbnail, so it is almost certainly a typo (e.g. "50M").
MIN_DOWNLOAD_BYTES: int = 1024

#: Placeholder credentials shipped in ``.env.example``. Bootstrapping with them
#: produces confusing AUTH_KEY_UNREGISTERED / API_ID_INVALID errors, so we
#: refuse to start and tell the operator exactly what to edit.
PLACEHOLDER_API_IDS: frozenset[str] = frozenset({"", "0", "123456"})
PLACEHOLDER_API_HASHES: frozenset[str] = frozenset(
    {
        "",
        "0123456789abcdef0123456789abcdef",
        "0123456789abcdef0123456789abcdef".upper(),
    }
)

#: Env keys merged (in this order) into ``monitored_chat_ids``. Duplicates are
#: dropped while preserving declaration order.
MONITORED_CHAT_ENV_KEYS: tuple[str, ...] = (
    "TG_MONITORED_CHAT_IDS",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ADMIN_CHAT_ID",
    "TELEGRAM_ADMIN_CHAT_IDS",
)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable, no side effects)
# --------------------------------------------------------------------------- #


def _env_str(name: str, default: str = "") -> str:
    """Return a stripped env var, treating unset/blank as ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    """Return an int env var, falling back silently to ``default`` when blank.

    Raises:
        SystemExit: if the variable holds a non-numeric value — silently
            ignoring it would hide a typo such as ``TG_HEARTBEAT_SECONDS=20s``.
    """
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"[config] {name}={raw!r} is not a valid integer. "
            "Fix it in .env or delete the line to use the default "
            f"({default})."
        ) from None


def _env_first(keys: tuple[str, ...], default: str = "") -> str:
    """Return the first non-blank env var among ``keys`` (``default`` if none)."""
    for key in keys:
        value = _env_str(key)
        if value:
            return value
    return default


def parse_size_bytes(raw: str) -> int:
    """Parse a human byte size: ``52428800``, ``50MB``, ``1.5 GiB``, ``800k``.

    Accepts plain integers (bytes) and the common suffixes ``B``/``K`+`/``M``/
    ``G`` with an optional trailing ``B`` or ``iB`` (binary multiples, matching
    ``MAX_DOWNLOAD_BYTES=…`` in ``.env``).

    Raises:
        ValueError: on an unparsable or negative value.
    """
    text = (raw or "").strip().strip("\"'").replace("_", "").replace(" ", "")
    if not text:
        raise ValueError("empty size")
    match = re.fullmatch(r"(?P<num>\d+(?:\.\d+)?)(?P<unit>[kKmMgGtT]?)(?:i?[bB])?", text)
    if match is None:
        raise ValueError(f"not a byte size: {raw!r}")
    number = float(match.group("num"))
    multiplier = {
        "": 1,
        "k": 1024,
        "m": 1024 * 1024,
        "g": 1024 * 1024 * 1024,
        "t": 1024 * 1024 * 1024 * 1024,
    }[match.group("unit").lower()]
    value = int(number * multiplier)
    if value < 0:
        raise ValueError(f"negative size: {raw!r}")
    return value


def parse_chat_ids(raw_values: list[str]) -> list[int | str]:
    """Parse one or more comma-separated chat-id lists into ints or names.

    ``-1001234567890`` -> ``int`` (fast numeric matching against
    ``event.chat_id``); anything else (``@my_channel``, ``merchant_alpha``)
    is kept as a raw string for case-insensitive title/username matching.

    Args:
        raw_values: Already-split env values; each may itself be a CSV list.

    Returns:
        Deduplicated list preserving first-seen order.
    """
    parsed: list[int | str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for token in raw.replace(";", ",").split(","):
            cleaned = token.strip().strip("\"'").strip()
            if not cleaned:
                continue
            # Accept Python-ish ints with underscores ("-1_001_234") too.
            numeric = cleaned.replace("_", "")
            value: int | str
            try:
                value = int(numeric)
            except ValueError:
                value = cleaned.lstrip("@")
            key = str(value).lower()
            if key in seen:
                continue
            seen.add(key)
            parsed.append(value)
    return parsed


def mask_secret(value: str, *, keep: int = 6) -> str:
    """Mask a secret for logs: first ``keep`` chars + ellipsis.

    Short/empty secrets never leak: they render as ``"<empty>"`` or
    ``"<redacted>"``.
    """
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "<redacted>"
    return f"{value[:keep]}… (len={len(value)})"


def resolve_path(raw: str, *, base: Path = REPO_ROOT) -> Path:
    """Resolve ``raw`` against ``base`` when it is relative; normalise it."""
    candidate = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip("\"'"))))
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def load_env_files() -> list[Path]:
    """Load ``<repo>/.env`` then ``./.env`` **without** overriding real env vars.

    Returns:
        The list of files that actually existed (for ``describe()``).
    """
    loaded: list[Path] = []
    if not _DOTENV_AVAILABLE:
        return loaded
    candidates = [REPO_ROOT / ".env"]
    try:
        cwd_env = Path.cwd() / ".env"
    except OSError:  # deleted working directory
        cwd_env = None  # type: ignore[assignment]
    if cwd_env is not None and cwd_env != candidates[0]:
        candidates.append(cwd_env)

    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        # override=False -> real process env always wins over the file.
        if load_dotenv(dotenv_path=path, override=False):
            loaded.append(path)
    return loaded


# --------------------------------------------------------------------------- #
# Config object
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkerConfig:
    """Immutable, fully validated worker configuration.

    Produced by :func:`load_config`; never mutated at runtime.
    """

    api_id: int
    api_hash: str = field(repr=False)
    session_string: str = field(default="", repr=False)
    session_file: str = DEFAULT_SESSION_FILE
    redis_url: str = "redis://localhost:6380/0"
    monitored_chat_ids: list[int | str] = field(default_factory=list)
    worker_name: str = ""
    heartbeat_seconds: int = 20
    reconnect_max_delay: int = 30
    #: Root of the media store (absolute, repo-root relative by default).
    media_root: str = DEFAULT_MEDIA_ROOT
    #: Ceiling for a single downloaded attachment, bytes (CONTRACTS_V2 §5).
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES

    # -- derived helpers --------------------------------------------------- #

    @property
    def has_session_string(self) -> bool:
        """True when a ``StringSession`` secret is configured."""
        return bool(self.session_string)

    @property
    def media_root_path(self) -> Path:
        """Absolute media store root (relative values resolved vs repo root)."""
        return resolve_path(self.media_root)

    @property
    def session_path(self) -> Path:
        """Absolute ``*.session`` path (relative values resolved vs repo root)."""
        return resolve_path(self.session_file)

    @property
    def session_file_exists(self) -> bool:
        """True when the on-disk Telethon session file is present."""
        try:
            return self.session_path.is_file()
        except OSError:
            return False

    @property
    def has_any_session(self) -> bool:
        """True when we can start non-interactively (string OR file)."""
        return self.has_session_string or self.session_file_exists

    @property
    def monitors_all_chats(self) -> bool:
        """Empty ``monitored_chat_ids`` means "listen to every group"."""
        return not self.monitored_chat_ids

    def describe(self) -> str:
        """Human/log friendly summary with **zero** secrets.

        ``api_id``/``api_hash``/``session_string`` are never rendered: only
        their presence is reported (``api_hash`` length, masked session).
        """
        chats = (
            "ALL groups (no filter)"
            if self.monitors_all_chats
            else ", ".join(str(c) for c in self.monitored_chat_ids)
        )
        return "\n".join(
            [
                "worker          = " + (self.worker_name or "<unnamed>"),
                "api_id          = <set>",
                f"api_hash        = <set, len={len(self.api_hash)}>",
                f"session_string  = {mask_secret(self.session_string)}",
                f"session_file    = {self.session_path} "
                f"({'exists' if self.session_file_exists else 'missing'})",
                f"redis_url       = {self.redis_url}",
                f"monitored_chats = {chats}",
                f"heartbeat_secs  = {self.heartbeat_seconds}",
                f"reconnect_max   = {self.reconnect_max_delay}s",
                f"media_root      = {self.media_root_path}",
                f"max_download    = {self.max_download_bytes} bytes "
                f"({self.max_download_bytes / (1024 * 1024):.1f} MiB)",
            ]
        )


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


def load_config(*, validate_credentials: bool = True) -> WorkerConfig:
    """Build a :class:`WorkerConfig` from env + ``.env`` files.

    Args:
        validate_credentials: When ``True`` (default) refuse to start with
            missing or placeholder ``TG_API_ID`` / ``TG_API_HASH``.
            ``--help`` / unit tests pass ``False``.

    Returns:
        A frozen ``WorkerConfig``.

    Raises:
        SystemExit: with an operator-actionable message when required values
            are missing or still hold the ``.env.example`` placeholders.
    """
    load_env_files()

    problems: list[str] = []

    # ---- Telegram credentials ------------------------------------------- #
    raw_api_id = _env_str("TG_API_ID") or _env_str("TELEGRAM_API_ID")
    raw_api_hash = _env_str("TG_API_HASH") or _env_str("TELEGRAM_API_HASH")

    api_id = 0
    if raw_api_id.lower() in PLACEHOLDER_API_IDS:
        problems.append(
            "TG_API_ID is unset or still the .env.example placeholder. Set the real "
            "numeric id from https://my.telegram.org -> API development tools."
        )
    else:
        try:
            api_id = int(raw_api_id)
        except ValueError:
            problems.append("TG_API_ID must be an integer (api_id from my.telegram.org).")
            api_id = 0

    # Never echo the hash itself: this message is logged by worker.main().
    if raw_api_hash.lower() in PLACEHOLDER_API_HASHES:
        problems.append(
            "TG_API_HASH is unset or still the .env.example placeholder. Copy the real "
            "32-character hash from https://my.telegram.org -> API development tools."
        )

    if problems and validate_credentials:
        raise SystemExit(
            "\n"
            + "=" * 78
            + "\nTelethon worker cannot start — credentials are not configured:\n  - "
            + "\n  - ".join(problems)
            + "\n\nFix: copy `.env.example` to `.env` in the repository root and set\n"
            "     TG_API_ID / TG_API_HASH (then generate a session, see README.md).\n"            + "=" * 78
            + "\n"
        )

    # ---- String session (either spelling) ------------------------------- #
    session_string = _env_str("TG_STRING_SESSION") or _env_str("TG_SESSION_STRING")

    # ---- Session file (repo-root relative) ------------------------------ #
    session_file = _env_str("TG_SESSION_FILE", DEFAULT_SESSION_FILE)

    # ---- Redis ----------------------------------------------------------- #
    redis_url = _env_str("REDIS_URL", "redis://localhost:6380/0")
    if "://" not in redis_url:
        problems.append(
            f"REDIS_URL={redis_url!r} is not a valid redis URL; expected e.g. "
            "redis://localhost:6380/0"
        )

    # ---- Monitored chats (merged from 4 legacy keys) --------------------- #
    chat_values = [os.environ.get(key, "") for key in MONITORED_CHAT_ENV_KEYS]
    monitored = parse_chat_ids(chat_values)

    # ---- Misc ------------------------------------------------------------ #
    try:
        hostname = socket.gethostname() or "tg-worker"
    except OSError:
        hostname = "tg-worker"
    worker_name = _env_str("TG_WORKER_NAME") or _env_str("WORKER_NAME") or hostname

    heartbeat_seconds = max(5, _env_int("TG_HEARTBEAT_SECONDS", 20))
    reconnect_max_delay = max(5, _env_int("TG_RECONNECT_MAX_DELAY", 30))

    # ---- Media store (CONTRACTS_V2 §5 / §7) ------------------------------ #
    media_root = _env_first(MEDIA_ROOT_ENV_KEYS, DEFAULT_MEDIA_ROOT)

    raw_max_download = _env_first(MAX_DOWNLOAD_ENV_KEYS)
    max_download_bytes = DEFAULT_MAX_DOWNLOAD_BYTES
    if raw_max_download:
        try:
            max_download_bytes = parse_size_bytes(raw_max_download)
        except ValueError:
            problems.append(
                f"MAX_DOWNLOAD_BYTES={raw_max_download!r} is not a byte size; "
                "use e.g. 52428800 or 50MB"
            )
        if max_download_bytes < MIN_DOWNLOAD_BYTES:
            problems.append(
                f"MAX_DOWNLOAD_BYTES={max_download_bytes} is too small for any "
                f"attachment (minimum {MIN_DOWNLOAD_BYTES}); using the default "
                f"({DEFAULT_MAX_DOWNLOAD_BYTES})"
            )
            max_download_bytes = DEFAULT_MAX_DOWNLOAD_BYTES

    if problems and validate_credentials:
        raise SystemExit(
            "\n" + "=" * 78 + "\nTelethon worker cannot start:\n  - "
            + "\n  - ".join(problems)
            + "\n" + "=" * 78 + "\n"
        )

    return WorkerConfig(
        api_id=api_id,
        api_hash=raw_api_hash,
        session_string=session_string,
        session_file=session_file,
        redis_url=redis_url,
        monitored_chat_ids=monitored,
        worker_name=worker_name,
        heartbeat_seconds=heartbeat_seconds,
        reconnect_max_delay=reconnect_max_delay,
        media_root=media_root,
        max_download_bytes=max_download_bytes,
    )


__all__ = [
    "DEFAULT_MAX_DOWNLOAD_BYTES",
    "DEFAULT_MEDIA_ROOT",
    "DEFAULT_SESSION_FILE",
    "MAX_DOWNLOAD_ENV_KEYS",
    "MEDIA_ROOT_ENV_KEYS",
    "MIN_DOWNLOAD_BYTES",
    "MONITORED_CHAT_ENV_KEYS",
    "REPO_ROOT",
    "WorkerConfig",
    "load_config",
    "load_env_files",
    "mask_secret",
    "parse_chat_ids",
    "parse_size_bytes",
    "resolve_path",
]
