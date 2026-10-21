"""Pure unit tests for the SLA engine, the Telegram ingest bridge and the typing lock.

No PostgreSQL, no Redis, no network: every test either exercises a pure function or drives an
async coroutine against a tiny in-memory fake. Run from the repo root with::

    python -m pytest -q backend/tests

or from ``backend/``::

    python -m pytest -q tests

The module-level ``importorskip`` calls keep the suite green on a machine where the backend
dependencies were never installed. ``asyncio.run`` is used instead of an async test plugin so the
suite runs identically with or without ``pytest-asyncio`` installed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
pytest.importorskip("sqlalchemy")
pytest.importorskip("redis")
pytest.importorskip("orjson")
pytest.importorskip("pydantic_settings")

from app.services import sla_engine as sla  # noqa: E402
from app.services import tg_ingest as ingest  # noqa: E402
from app.services import typing as typing_service  # noqa: E402

UTC = UTC
NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def run(coro: Any) -> Any:
    """Execute one coroutine to completion (no third-party async wrapper needed)."""
    return asyncio.run(coro)


# ======================================================================= sla_percent
@pytest.mark.parametrize(
    ("resolved", "on_time", "expected"),
    [
        (0, 0, 100.0),  # no data -> perfect score, never 0.0 / NaN
        (0, 5, 100.0),  # on_time without resolves is still "no data"
        (10, 10, 100.0),
        (3, 2, 66.7),  # 66.666... rounds half-up to one decimal
        (3, 1, 33.3),
        (6, 5, 83.3),
        (2, 1, 50.0),
        (10, 9, 90.0),
        (12, 12, 100.0),
        (99, 99, 100.0),
        (7, 0, 0.0),
    ],
)
def test_sla_percent_rounds_to_one_decimal(resolved: int, on_time: int, expected: float) -> None:
    assert sla.sla_percent(resolved, on_time) == expected


def test_sla_percent_is_never_negative_nor_above_100() -> None:
    """A bad counter can neither produce a negative score nor exceed 100 %."""
    assert sla.sla_percent(-5, 3) == 100.0
    assert sla.sla_percent(-1, -1) == 100.0
    assert sla.sla_percent(10, 999) == 100.0
    assert sla.sla_percent(10, -3) == 0.0


def test_sla_percent_always_has_at_most_one_decimal() -> None:
    for resolved in range(1, 40):
        for on_time in range(0, resolved + 1):
            value = sla.sla_percent(resolved, on_time)
            assert isinstance(value, float)
            assert round(value, 1) == value
            assert 0.0 <= value <= 100.0


def test_sla_percent_half_up_not_bankers() -> None:
    """1/8 = 12.5 % must round up (``round()`` on floats would also do it, but be explicit)."""
    assert sla.sla_percent(8, 1) == 12.5
    # 17/18 = 94.444.. -> 94.4 ; 18/19 = 94.736.. -> 94.7
    assert sla.sla_percent(18, 17) == 94.4
    assert sla.sla_percent(19, 18) == 94.7


# ======================================================================= response_seconds
def test_response_seconds_whole_seconds() -> None:
    assert sla.response_seconds(NOW, NOW + timedelta(seconds=75)) == 75


def test_response_seconds_truncates_fractions() -> None:
    """Sub-second precision is truncated, never rounded up."""
    assert sla.response_seconds(NOW, NOW + timedelta(seconds=75, microseconds=999_000)) == 75


def test_response_seconds_negative_delta_clamps_to_zero() -> None:
    """Clock skew between two writers must never yield a negative SLA."""
    assert sla.response_seconds(NOW, NOW - timedelta(seconds=30)) == 0
    assert sla.response_seconds(NOW, NOW) == 0


def test_response_seconds_none_is_zero() -> None:
    assert sla.response_seconds(None, NOW) == 0
    assert sla.response_seconds(NOW, None) == 0


def test_response_seconds_accepts_naive_datetimes() -> None:
    """Naive values (SQLite / test fakes) are assumed UTC instead of raising ``TypeError``."""
    naive_start = NOW.replace(tzinfo=None)
    naive_end = (NOW + timedelta(seconds=42)).replace(tzinfo=None)
    assert sla.response_seconds(naive_start, naive_end) == 42
    assert sla.response_seconds(naive_start, NOW + timedelta(seconds=42)) == 42


# ======================================================================= remaining / overdue
def test_remaining_seconds_positive_and_negative() -> None:
    assert sla.remaining_seconds(NOW + timedelta(seconds=120), NOW) == 120
    assert sla.remaining_seconds(NOW - timedelta(seconds=1), NOW) == -1


def test_remaining_seconds_floors_instead_of_truncating() -> None:
    """A 119.6 s countdown must read 119, not 120 (the client must never see more time than left)."""
    deadline = NOW + timedelta(seconds=119, microseconds=600_000)
    assert sla.remaining_seconds(deadline, NOW) == 119


def test_remaining_seconds_none_deadline_is_zero() -> None:
    assert sla.remaining_seconds(None, NOW) == 0


def test_is_overdue_follows_the_deadline() -> None:
    assert sla.is_overdue(NOW - timedelta(seconds=1), NOW) is True
    assert sla.is_overdue(NOW + timedelta(seconds=1), NOW) is False


def test_remaining_seconds_normalises_mixed_timezones() -> None:
    plus_two = timezone(timedelta(hours=2))
    deadline = (NOW + timedelta(seconds=90)).astimezone(plus_two)
    assert sla.remaining_seconds(deadline, NOW) == 90


def test_burning_threshold_is_60_seconds() -> None:
    assert sla.BURNING_THRESHOLD_SECONDS == 60
    assert sla._is_burning(NOW + timedelta(seconds=60), NOW) is True
    assert sla._is_burning(NOW + timedelta(seconds=61), NOW) is False
    assert sla._is_burning(NOW - timedelta(seconds=5), NOW) is True


# ======================================================================= deadline quantisation
def test_next_multiple_of_tick_rounds_up_to_the_tick() -> None:
    """A 300 s target opened mid-tick lands on a round deadline (the §3.3 "120 s" example)."""
    quantised = sla._next_multiple_of_tick(timedelta(seconds=300, microseconds=400_000))
    total = quantised.total_seconds()
    assert total >= 300
    assert total % sla.settings.backend_ws_tick_seconds == 0


def test_next_multiple_of_tick_keeps_exact_multiples() -> None:
    exact = timedelta(seconds=300)
    assert sla._next_multiple_of_tick(exact).total_seconds() == 300


def test_next_multiple_of_tick_never_returns_negative() -> None:
    assert sla._next_multiple_of_tick(timedelta(seconds=-5)).total_seconds() >= 0


# ======================================================================= SLA target resolution
def test_sla_target_prefers_the_operator_override() -> None:
    operator = SimpleNamespace(sla_target_seconds=120)
    assert sla._sla_target_for(operator) == 120


def test_sla_target_falls_back_to_global_settings() -> None:
    assert sla._sla_target_for(None) == sla.settings.backend_sla_seconds
    assert sla.settings.backend_sla_seconds == 300


def test_sla_target_ignores_invalid_values() -> None:
    """``0``, negative and ``None`` targets must never produce a zero-length SLA window."""
    assert sla._sla_target_for(SimpleNamespace(sla_target_seconds=0)) == 300
    assert sla._sla_target_for(SimpleNamespace(sla_target_seconds=-30)) == 300
    assert sla._sla_target_for(SimpleNamespace(sla_target_seconds=None)) == 300
    assert sla._sla_target_for(SimpleNamespace(sla_target_seconds="oops")) == 300


# ======================================================================= assignment selection
def test_pick_least_loaded_prefers_the_lowest_queue() -> None:
    loads = {1: 3, 2: 1, 3: 2}
    assert sla._pick_least_loaded([1, 2, 3], loads) == 2


def test_pick_least_loaded_deterministic_tie_break() -> None:
    """With the deterministic strategy the lowest id wins every tie (used by the tests/ops)."""
    loads = {1: 1, 2: 1, 3: 5}
    for _ in range(10):
        assert sla._pick_least_loaded([1, 2, 3], loads, sla.LOWEST_ID_LEAST_LOADED) == 1


def test_pick_least_loaded_rotates_ties_round_robin() -> None:
    """Empty queues must not pin every chat to one operator: the cursor rotates the winner."""
    loads = {1: 0, 2: 0, 3: 0}
    picks = {sla._pick_least_loaded([1, 2, 3], loads) for _ in range(12)}
    assert picks == {1, 2, 3}


def test_pick_least_loaded_handles_no_candidates() -> None:
    assert sla._pick_least_loaded([], {}) is None


def test_pick_least_loaded_treats_missing_counts_as_zero() -> None:
    assert sla._pick_least_loaded([4, 5], {4: 2}) in (5,)


def test_least_loaded_strategy_prefers_less_served_operators() -> None:
    assert sla.least_loaded_strategy("RESOLVED", 3) == 3
    assert sla.least_loaded_strategy("PENDING", 3) == 0


# ======================================================================= status normalisation
def test_status_value_unwraps_str_enums() -> None:
    from app.models import MessageSenderType, ShiftStatus

    assert sla._status_value(ShiftStatus.ONLINE) == "ONLINE"
    assert sla._status_value(MessageSenderType.BOT) == "BOT"
    assert sla._status_value("PENDING") == "PENDING"
    assert sla._status_value(None) == ""


# ======================================================================= SLA_UPDATE payload
def test_sla_update_payload_matches_contract_keys() -> None:
    incident = SimpleNamespace(
        id=9,
        chat_id=1,
        status="PENDING",
        deadline_at=NOW + timedelta(seconds=120),
        assigned_operator_id=2,
        assigned_operator_name="Operator Two",
    )
    payload = sla.sla_update_payload(incident, now=NOW)
    assert set(payload) == {
        "chat_id",
        "incident_id",
        "status",
        "deadline_at",
        "remaining_seconds",
        "assigned_operator_id",
        "assigned_operator_name",
        "is_overdue",
    }
    assert payload["chat_id"] == 1
    assert payload["incident_id"] == 9
    assert payload["status"] == "PENDING"
    assert payload["deadline_at"] == "2024-06-01T12:02:00Z"
    assert payload["remaining_seconds"] == 120
    assert payload["assigned_operator_id"] == 2
    assert payload["assigned_operator_name"] == "Operator Two"
    assert payload["is_overdue"] is False


def test_sla_update_payload_marks_overdue() -> None:
    incident = SimpleNamespace(
        id=4,
        chat_id=7,
        status="PENDING",
        deadline_at=NOW - timedelta(seconds=30),
        assigned_operator_id=None,
        assigned_operator_name=None,
    )
    payload = sla.sla_update_payload(incident, now=NOW)
    assert payload["remaining_seconds"] == -30
    assert payload["is_overdue"] is True
    assert payload["assigned_operator_id"] is None
    assert payload["assigned_operator_name"] is None


def test_sla_update_payload_falls_back_to_the_chat_owner_name() -> None:
    incident = SimpleNamespace(
        id=4,
        chat_id=7,
        status="PENDING",
        deadline_at=NOW,
        assigned_operator_id=None,
        assigned_operator_name=None,
    )
    chat = SimpleNamespace(id=7, assigned_operator_name="Operator Seven")
    payload = sla.sla_update_payload(incident, chat=chat, now=NOW)
    assert payload["assigned_operator_name"] == "Operator Seven"


def test_chat_assigned_payload_shape() -> None:
    chat = SimpleNamespace(id=3, assigned_operator_id=5, assigned_operator_name="Operator Five")
    payload = sla.chat_assigned_payload(chat)
    assert payload == {
        "chat_id": 3,
        "assigned_operator_id": 5,
        "assigned_operator_name": "Operator Five",
    }


# ======================================================================= tick: never raises
class _BrokenFactory:
    """Session factory whose constructor raises — the worst case for the background ticker."""

    def __call__(self) -> Any:
        raise RuntimeError("database is on fire")


def test_tick_swallows_database_failures() -> None:
    """``tick`` must never propagate: the ticker would restart with a crash loop otherwise."""
    run(sla.tick(_BrokenFactory(), None))


class _BrokenBroadcast:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        raise ConnectionResetError("client vanished")


def test_tick_swallows_failing_session_factory_with_broadcast() -> None:
    broadcast = _BrokenBroadcast()
    run(sla.tick(_BrokenFactory(), broadcast))


# ======================================================================= ingest helpers
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Merchant Alpha — Payouts", "MRC-ALPHA"),
        ("Merchant Zeta — Integrations / API", "MRC-ZETA"),
        ("Merchant Beta — Settlements", "MRC-BETA"),
        ("Acme Payouts", "MRC-ACME"),
        ("merchant delta - KYC", "MRC-DELTA"),
    ],
)
def test_derive_merchant_code_from_title(title: str, expected: str) -> None:
    assert ingest.derive_merchant_code(title) == expected


def test_derive_merchant_code_falls_back_to_chat_id() -> None:
    assert ingest.derive_merchant_code("", -1001234567890) == "MRC-567890"
    assert ingest.derive_merchant_code(None, -1001234567890) == "MRC-567890"
    assert ingest.derive_merchant_code(None, None) is None


def test_derive_merchant_code_is_uppercase_and_short() -> None:
    code = ingest.derive_merchant_code("merchant supercalifragilisticexpialidocious")
    assert code is not None
    assert code.startswith("MRC-")
    assert code == code.upper()
    assert len(code) <= 28


def test_coerce_int_rejects_bools_and_junk() -> None:
    assert ingest._coerce_int("90210") == 90210
    assert ingest._coerce_int(90210) == 90210
    assert ingest._coerce_int(True) is None
    assert ingest._coerce_int("not-a-number") is None
    assert ingest._coerce_int(None) is None


def test_parse_tg_datetime_handles_both_iso_flavours() -> None:
    assert ingest._parse_tg_datetime("2024-06-01T12:00:00+00:00") == NOW
    assert ingest._parse_tg_datetime("2024-06-01T12:00:00Z") == NOW
    assert ingest._parse_tg_datetime("garbage") is None
    assert ingest._parse_tg_datetime(None) is None


def test_parse_tg_datetime_keeps_awareness() -> None:
    parsed = ingest._parse_tg_datetime("2024-06-01T12:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_enum_value_unwraps_delivery_status() -> None:
    from app.models import DeliveryStatus

    assert ingest._enum_value(DeliveryStatus.SENT) == "sent"
    assert ingest._enum_value(DeliveryStatus.FAILED) == "failed"
    assert ingest._enum_value("pending") == "pending"


# ======================================================================= ingest handlers: guards
def test_handle_incoming_ignores_non_dict_payloads() -> None:
    """A malformed frame must be dropped, never raise into the Redis consumer."""
    run(ingest.handle_incoming(_BrokenFactory(), None, ["not", "a", "dict"]))  # type: ignore[arg-type]
    run(ingest.handle_incoming(_BrokenFactory(), None, None))  # type: ignore[arg-type]


def test_handle_deleted_noop_on_empty_ids() -> None:
    """Telegram sends an empty ``tg_msg_ids`` for a full delete: the handler must stay quiet."""
    run(
        ingest.handle_deleted(
            _BrokenFactory(),
            None,
            {"kind": "MESSAGE_DELETED", "tg_chat_id": -1001234567890, "tg_msg_ids": []},
        )
    )


def test_handle_outgoing_ack_requires_message_id() -> None:
    run(ingest.handle_outgoing_ack(_BrokenFactory(), None, {"ok": True}))
    run(ingest.handle_outgoing_ack(_BrokenFactory(), None, {"message_id": 55, "ok": True}))


def test_handle_edit_requires_ids() -> None:
    run(ingest.handle_edit(_BrokenFactory(), None, {"text": "no ids"}))
    run(ingest.handle_edit(_BrokenFactory(), None, {"tg_chat_id": -1, "tg_msg_id": 2, "text": "x"}))


def test_all_ingest_handlers_exist_with_the_documented_signature() -> None:
    """``background.redis_consumer`` calls ``handler(session_factory, broadcast, payload)``."""
    import inspect

    for name in ("handle_incoming", "handle_deleted", "handle_edit", "handle_outgoing_ack"):
        handler = getattr(ingest, name)
        assert callable(handler), name
        parameters = list(inspect.signature(handler).parameters.values())
        assert len(parameters) >= 3, name


# ======================================================================= typing helpers
def test_display_name_prefers_display_name_then_username() -> None:
    assert typing_service._display_name(SimpleNamespace(id=2, display_name="Operator Two")) == (
        "Operator Two"
    )
    assert typing_service._display_name(SimpleNamespace(id=7, display_name="", username="op7")) == (
        "op7"
    )
    assert typing_service._display_name(SimpleNamespace(id=9, display_name="   ")) == "Operator #9"


def test_describe_lock_builds_the_409_body() -> None:
    assert typing_service.describe_lock({"operator_id": 2, "operator_name": "Operator Two"}) == (
        "Operator Two"
    )
    assert typing_service.describe_lock({"operator_id": 4, "operator_name": None}) == "Operator #4"
    assert typing_service.describe_lock(None) == ""


def test_typing_lock_ttl_matches_the_contract() -> None:
    assert typing_service.TYPING_LOCK_TTL_SECONDS == 15


def test_handle_typing_event_is_defensive_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis down must answer permissively (``ok=True``) instead of hiding the chat."""

    async def _unavailable(chat_id: int) -> None:
        return None

    async def _acquire(chat_id: int, operator_id: int, ttl: int = 15) -> bool:
        return False

    async def _noop_broadcast(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(typing_service, "get_typing_lock", _unavailable)
    monkeypatch.setattr(typing_service, "set_typing_lock", _acquire)
    monkeypatch.setattr(typing_service, "_broadcast", _noop_broadcast)

    operator = SimpleNamespace(id=2, display_name="Operator Two")
    result = run(typing_service.handle_typing_event(operator, 1, "start"))
    assert result["ok"] is True
    assert result["locked_by"] is None
    assert result["chat_id"] == 1


def test_handle_typing_event_reports_a_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second operator hitting ``start`` gets ``ok=False`` plus the holder's display name."""
    broadcast_calls: list[tuple[str, dict[str, Any]]] = []

    async def _held_by_operator_three(chat_id: int) -> int:
        return 3

    async def _acquire(chat_id: int, operator_id: int, ttl: int = 15) -> bool:
        raise AssertionError("must not write the lock when it is held by somebody else")

    async def _name(operator_id: int, **_kwargs: Any) -> str | None:
        return {3: "Operator Three"}.get(operator_id)

    async def _record(event_type: str, data: dict[str, Any]) -> None:
        broadcast_calls.append((event_type, data))

    monkeypatch.setattr(typing_service, "get_typing_lock", _held_by_operator_three)
    monkeypatch.setattr(typing_service, "set_typing_lock", _acquire)
    monkeypatch.setattr(typing_service, "operator_display_name", _name)
    monkeypatch.setattr(typing_service, "_broadcast", _record)

    operator = SimpleNamespace(id=2, display_name="Operator Two")
    result = run(typing_service.handle_typing_event(operator, 1, "start"))
    assert result == {"ok": False, "locked_by": "Operator Three", "chat_id": 1}
    assert broadcast_calls and broadcast_calls[0][0] == "TYPING_LOCK"
    assert broadcast_calls[0][1]["operator_id"] == 3


def test_handle_typing_event_stop_releases_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    broadcast_calls: list[tuple[str, dict[str, Any]]] = []
    cleared: list[tuple[int, int]] = []

    async def _clear(chat_id: int, operator_id: int | None = None) -> bool:
        cleared.append((chat_id, int(operator_id or 0)))
        return True

    async def _record(event_type: str, data: dict[str, Any]) -> None:
        broadcast_calls.append((event_type, data))

    monkeypatch.setattr(typing_service, "clear_typing_lock", _clear)
    monkeypatch.setattr(typing_service, "_broadcast", _record)

    operator = SimpleNamespace(id=5, display_name="Operator Five")
    result = run(typing_service.handle_typing_event(operator, 12, "stop"))
    assert result == {"ok": True, "locked_by": None, "chat_id": 12}
    assert cleared == [(12, 5)]
    assert broadcast_calls == [("TYPING_UNLOCK", {"chat_id": 12, "operator_id": 5})]


def test_handle_typing_event_start_grants_the_lock_when_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _free(chat_id: int) -> None:
        return None

    async def _acquire(chat_id: int, operator_id: int, ttl: int = 15) -> bool:
        assert ttl == 15
        return True

    async def _noop_broadcast(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(typing_service, "get_typing_lock", _free)
    monkeypatch.setattr(typing_service, "set_typing_lock", _acquire)
    monkeypatch.setattr(typing_service, "_broadcast", _noop_broadcast)

    operator = SimpleNamespace(id=2, display_name="Operator Two")
    result = run(typing_service.handle_typing_event(operator, 1, "start"))
    assert result["ok"] is True
    assert result["locked_by"] is None


def test_handle_typing_event_never_raises_on_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _explode(chat_id: int) -> int:
        raise RuntimeError("redis protocol error")

    async def _noop_broadcast(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(typing_service, "get_typing_lock", _explode)
    monkeypatch.setattr(typing_service, "_broadcast", _noop_broadcast)

    operator = SimpleNamespace(id=2, display_name="Operator Two")
    result = run(typing_service.handle_typing_event(operator, 1, "start"))
    assert result["ok"] is True
    assert result["locked_by"] is None


# ======================================================================= module surface
def test_pending_incident_query_forces_a_fresh_read() -> None:
    """The PENDING lookup must use ``populate_existing``.

    Regression guard: the guarded ``UPDATE`` in ``resolve_for_reply`` bypasses the ORM, so without
    this option an incident resolved earlier in the same session still looks PENDING in the
    identity map and a new inbound message would be silently absorbed instead of opening a clock.
    """
    import inspect

    source = inspect.getsource(sla._pending_incident)
    assert "populate_existing=True" in source


def test_resolve_reloads_through_the_identity_map() -> None:
    """``resolve_for_reply`` must refresh the returned row (stale payload regression guard)."""
    import inspect

    source = inspect.getsource(sla.resolve_for_reply)
    assert "populate_existing=True" in source


def test_tick_escalation_uses_a_guarded_update() -> None:
    """Escalation must persist the flag regardless of flush ordering."""
    import inspect

    source = inspect.getsource(sla.tick_incidents)
    assert "escalated=True" in source
    assert "is_(False)" in source or "is_(false" in source.lower()


def test_sla_engine_exports_the_documented_api() -> None:
    for name in (
        "open_incident",
        "assign_round_robin",
        "resolve_for_reply",
        "personal_stats",
        "tick",
        "sla_summary",
        "burning_chat_ids",
        "supervisor_dashboard",
        "sla_percent",
        "response_seconds",
    ):
        assert callable(getattr(sla, name)), name


def test_typing_service_exports_the_documented_api() -> None:
    for name in ("handle_typing_event", "release_chat_lock", "get_lock_info"):
        assert callable(getattr(typing_service, name)), name
