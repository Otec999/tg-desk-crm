"""Small, dependency-free helpers shared by the backend layers.

This module is deliberately **import-free with respect to the rest of the
application** (no ``app.config`` / ``app.database`` / ``app.models`` imports) so
that it can be imported from models, schemas, security, services, routers, the
seed script and the tests without creating an import cycle.

Everything here is deterministic, platform independent and safe to call from
async code (no blocking I/O, no global mutable state).
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, TypeVar, overload

__all__ = [
    "MAX_TELEGRAM_TEXT_LENGTH",
    "TWO_PLACES",
    "clamp",
    "clamp_int",
    "coerce_aware_utc",
    "extract_phone",
    "extract_tx_id",
    "format_decimal_2dp",
    "format_duration_short",
    "iso_z",
    "mock_rng",
    "normalize_phone",
    "normalize_tx_id",
    "round_sla_percent",
    "sanitize_telegram_text",
    "stable_hash_int",
    "utcnow",
]

#: Hard limit enforced by Telegram itself for a single text message.
MAX_TELEGRAM_TEXT_LENGTH: Final[int] = 4096

#: Quantizer for money-like decimal strings (2 decimal places).
TWO_PLACES: Final[Decimal] = Decimal("0.01")

Number = TypeVar("Number", int, float)

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    """Serialise a datetime as ISO-8601 with a literal ``Z`` suffix.

    Naive datetimes are interpreted as UTC (PostgreSQL ``TIMESTAMP WITH TIME
    ZONE`` round-trips as aware, but SQLite / tests may hand back naive values).
    Returns ``None`` for ``None`` so callers can use it for nullable columns.
    """
    if dt is None:
        return None
    aware = coerce_aware_utc(dt)
    text = aware.isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def coerce_aware_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a timezone-aware UTC datetime.

    Naive inputs are *assumed* to already be UTC, which is what asyncpg returns
    when a column is declared as ``TIMESTAMP WITH TIME ZONE`` in a database whose
    session timezone is UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


@overload
def clamp(value: int, lo: int, hi: int) -> int: ...


@overload
def clamp(value: float, lo: float, hi: float) -> float: ...


def clamp(value: int | float, lo: int | float, hi: int | float) -> int | float:
    """Bound ``value`` to the inclusive ``[lo, hi]`` range.

    ``lo`` is assumed to be ``<= hi``; if a caller passes an inverted range the
    bounds are swapped instead of raising, because this helper is used on
    untrusted inputs (SLA counters, pagination limits) where raising would turn
    a cosmetic problem into a 500.
    """
    if lo > hi:
        lo, hi = hi, lo
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def clamp_int(value: int, lo: int, hi: int) -> int:
    """Integer-specialised :func:`clamp` that never returns a float."""
    result = clamp(value, lo, hi)
    return int(result)


def round_sla_percent(resolved: int, on_time: int) -> float:
    """Return ``on_time / resolved`` as a percentage rounded to one decimal.

    ``resolved <= 0`` yields ``100.0``: an operator who has never had an SLA
    incident has not missed one, so the UI shows a perfect score instead of
    ``0.0`` (which would look like a failure) or ``NaN`` (invalid JSON).
    """
    if resolved <= 0:
        return 100.0
    ratio = (float(on_time) / float(resolved)) * 100.0
    return round(clamp(ratio, 0.0, 100.0), 1)


def format_decimal_2dp(value: object) -> str:
    """Render a money-like value as a decimal string with exactly two places.

    Accepts ``Decimal`` (``Numeric(18, 2)`` columns), ``int``, ``float`` or a
    numeric string. ``None`` becomes ``"0.00"`` so a JSON schema that promises a
    string never receives ``null``. ``ROUND_HALF_UP`` is used explicitly to
    avoid banker's rounding surprises in provider payloads.
    """
    if value is None:
        return "0.00"
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        try:
            decimal_value = Decimal(str(value).strip() or "0")
        except (InvalidOperation, ValueError):
            return "0.00"
    if not decimal_value.is_finite():
        return "0.00"
    return str(decimal_value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def format_duration_short(seconds: int | float | None) -> str:
    """Format a duration as a short human string, e.g. ``"1m 15s"``.

    Rules: ``< 1s`` -> ``"0s"``; ``< 60s`` -> ``"45s"``; ``< 1h`` ->
    ``"1m 15s"`` (seconds omitted when zero), ``< 1d`` -> ``"2h 05m"``,
    otherwise ``"1d 03h"``. Negative inputs are clamped to ``0``.
    """
    if seconds is None:
        return "0s"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0s"
    total = max(total, 0)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    if total < 86400:
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}h {minutes:02d}m"
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days}d {hours:02d}h"


# ---------------------------------------------------------------------------
# Deterministic hashing / RNG (mock provider adapters)
# ---------------------------------------------------------------------------


def stable_hash_int(value: str) -> int:
    """Return a deterministic, non-negative integer hash of ``value``.

    Uses SHA-256 (never Python's randomised ``hash()``), so the same input maps
    to the same integer on every machine, every process and every restart. That
    is what makes the mock admin panels and the seeded mock data reproducible.
    """
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def mock_rng(tx_id: str, salt: str = "") -> random.Random:
    """Return a :class:`random.Random` seeded deterministically from the inputs.

    ``salt`` allows one transaction id to drive several independent mock streams
    (e.g. ``salt="ADMIN_ONE"`` vs ``salt="ADMIN_TWO"``) while staying stable
    across processes.
    """
    return random.Random(stable_hash_int(f"{tx_id}\x00{salt}"))


# ---------------------------------------------------------------------------
# Transaction-id extraction
# ---------------------------------------------------------------------------

# Canonical form is uppercase. Accepted inputs (case-insensitive):
#   TX-4412-DD    (prefix + numeric core + optional 1-2 letter suffix)
#   TX-1234       (prefix + numeric core)
#   tx:4412DD     (colon separator)
#   TX 4412 DD    (space separator, as pasted from a support ticket)
#   TX4412DD      (no separator at all)
#   <code>TX-4412-DD</code> / "`TX-4412-DD`" / "(TX-4412-DD)" wrappers
_TX_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9A-Za-z])TX[\s:_#\-]*([0-9]{4,12})(?:[\s\-_]?([A-Z]{1,2}))?(?![0-9A-Za-z])",
    re.IGNORECASE,
)


def normalize_tx_id(value: str | None) -> str | None:
    """Normalise any accepted transaction-id spelling to its canonical form.

    Returns ``"TX-4412-DD"`` for ``"tx:4412dd"``, ``"TX-1234"`` for
    ``"tx 1234"``. Returns ``None`` when ``value`` is not a transaction id.
    """
    if not value or not isinstance(value, str):
        return None
    match = _TX_ID_RE.search(value)
    if match is None:
        return None
    core, suffix = match.group(1), match.group(2)
    canonical = f"TX-{core}"
    if suffix:
        canonical = f"{canonical}-{suffix.upper()}"
    return canonical


def extract_tx_id(text: str) -> str | None:
    """Find the first transaction id inside free-form text.

    Returns the uppercase canonical form (e.g. ``"TX-4412-DD"``) or ``None``
    when the text contains no transaction id. HTML-ish wrappers such as
    ``<code>TX-4412-DD</code>`` are handled because the pattern only anchors on
    non-alphanumeric boundaries.
    """
    return normalize_tx_id(text)


# ---------------------------------------------------------------------------
# Phone extraction
# ---------------------------------------------------------------------------

# International-ish: optional '+', then a digit-led run of digits/spaces/dashes/
# dots/parentheses holding 8..20 digits total. Guards against eating surrounding
# words or arbitrary long digit runs (ids, timestamps).
_PHONE_CANDIDATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9A-Za-z])(?:\+\s?)?\d[\d\s().\-]{6,20}\d(?![0-9A-Za-z])"
)
_PHONE_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\d+")


def normalize_phone(value: str | None) -> str | None:
    """Normalise a phone number to ``"+<digits>"`` (E.164-ish, no separators).

    Keeps a leading ``+`` when the source had one, otherwise restores it when the
    number looks international (>= 11 digits, a common dialling assumption).
    Returns ``None`` when the value does not carry a plausible phone number.
    """
    if not value or not isinstance(value, str):
        return None
    digits = "".join(_PHONE_DIGITS_RE.findall(value))
    if len(digits) < 8 or len(digits) > 15:
        return None
    # Strip a leading international access prefix duplicated by callers: "+" is
    # handled below; a literal "00" prefix is the PSTN equivalent of "+".
    if digits.startswith("00") and len(digits) > 10:
        digits = digits[2:]
    return f"+{digits}"


def extract_phone(text: str) -> str | None:
    """Find the first phone number inside free-form text.

    Returns a ``"+<digits>"`` string or ``None``. Digit runs shorter than 8 or
    longer than 15 digits (transaction cores, ids, timestamps) are rejected.
    """
    if not text or not isinstance(text, str):
        return None
    for candidate in _PHONE_CANDIDATE_RE.finditer(text):
        normalised = normalize_phone(candidate.group(0))
        if normalised is not None:
            return normalised
    return None


# ---------------------------------------------------------------------------
# Text sanitisation
# ---------------------------------------------------------------------------

# Control characters survive in Telegram payloads (RTL overrides, zero-width
# joiners, NUL from malformed clients). We keep \n and \t, drop the rest of the
# Cc/Cf ranges, and collapse >2 consecutive newlines which Telegram rejects.
_CONTROL_CHARS_RE: Final[re.Pattern[str]] = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)
_EXCESS_NEWLINES_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")


def sanitize_telegram_text(text: str) -> str:
    """Make ``text`` safe to store and forward to Telegram.

    * strips NUL and control characters except ``\\n`` and ``\\t``,
    * normalises CRLF/CR to ``\\n`` and collapses 3+ blank lines into 2,
    * caps the result at :data:`MAX_TELEGRAM_TEXT_LENGTH` (4096) characters,
    * ``.strip()``\\ s leading/trailing whitespace.

    Never raises: non-string input yields ``""``.
    """
    if not text or not isinstance(text, str):
        return ""
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = _EXCESS_NEWLINES_RE.sub("\n\n", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > MAX_TELEGRAM_TEXT_LENGTH:
        cleaned = cleaned[:MAX_TELEGRAM_TEXT_LENGTH].rstrip()
    return cleaned
