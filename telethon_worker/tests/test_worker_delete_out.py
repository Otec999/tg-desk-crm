"""Tests for the ``tg_delete_out`` command (CONTRACTS_V5 §1.4 / §1.5 / §1.6).

The worker must turn a backend deletion command into

    channels.DeleteMessagesRequest(channel=<peer>, id=[tg_msg_id])

and answer on ``tg_outgoing_ack`` with ``cmd_kind="DELETE"``. A message that
Telegram already considers gone (``MessageDeleteForbiddenError``) is a success:
the operator's goal — "it must not be visible" — is reached.

No network, no Redis, no real Telethon session: the client is a recording
double exposing only ``get_entity`` + ``__call__``, and the bus collects the
published envelopes.

Run from ``telethon_worker/``::

    ..\\.venv\\Scripts\\python.exe -m pytest -q
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

# ``worker.py`` lives one level up and is imported exactly as the worker itself
# imports it (``python worker.py`` from ``telethon_worker/``).
WORKER_DIR = Path(__file__).resolve().parent.parent
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

# Same reason as ``test_media.py``: this file may be run on its own, so the
# asyncio auto mode is forced here instead of relying on a pytest config.
pytest_plugins = ("pytest_asyncio",)
DEFAULT_FIXTURE_LOOP_SCOPE = "function"


def pytest_configure(config: Any) -> None:
    """Enable pytest-asyncio's auto mode explicitly."""
    config.option.asyncio_mode = "auto"


from telethon.errors import (  # noqa: E402
    FloodWaitError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
)
from telethon.tl.functions.channels import DeleteMessagesRequest  # noqa: E402

import worker  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants under test
# --------------------------------------------------------------------------- #

CHANNEL_DELETE_OUT = "tg_delete_out"
CHANNEL_OUTGOING_ACK = "tg_outgoing_ack"
TG_CHAT_ID = -1004338873193
#: What ``_resolve_entity`` returns for the fake client, so ``channel`` must be
#: this entity — never a re-derived peer.
FAKE_PEER = "resolved-peer"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeBus:
    """Collects published payloads instead of talking to Redis."""

    def __init__(self) -> None:
        #: ``worker._PublishStats`` is a dataclass, so reusing the real class
        #: keeps the ``delete_out`` assertions honest (the double invents no
        #: attribute the production class does not have).
        self.stats = worker._PublishStats()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        self.published.append((channel, payload))
        return True

    def acks(self) -> list[dict[str, Any]]:
        """Only the ``tg_outgoing_ack`` envelopes, in publish order."""
        return [payload for channel, payload in self.published
                if channel == CHANNEL_OUTGOING_ACK]


class RecordingClient:
    """Telethon lookalike: ``get_entity`` plus the ``client(request)`` RPC call.

    ``__call__`` is the only RPC entry point the worker uses, so the double
    records every ``DeleteMessagesRequest`` and can raise a scripted error.
    """

    def __init__(
        self,
        *,
        entity: Any = FAKE_PEER,
        raise_exc: Optional[BaseException] = None,
        connected: bool = True,
    ) -> None:
        self.entity = entity
        self.raise_exc = raise_exc
        self._connected = connected
        self.entity_requests: list[Any] = []
        self.rpc_calls: list[Any] = []

    def is_connected(self) -> bool:
        return self._connected

    async def get_entity(self, chat_id: Any) -> Any:
        self.entity_requests.append(chat_id)
        return self.entity

    async def __call__(self, request: Any) -> Any:
        self.rpc_calls.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return types.SimpleNamespace(pts=1, pts_count=1)

    def delete_requests(self) -> list[Any]:
        return [call for call in self.rpc_calls
                if isinstance(call, DeleteMessagesRequest)]


@pytest.fixture()
def worker_config(tmp_path: Path) -> Any:
    """A minimal, credential-free ``WorkerConfig``."""
    import config as worker_config_module  # noqa: PLC0415

    return worker_config_module.WorkerConfig(
        api_id=1,
        api_hash="x" * 32,
        media_root=str(tmp_path / "media"),
        max_download_bytes=1024 * 1024,
    )


def _make_worker(cfg: Any) -> Any:
    """A ``TelethonWorker`` with a collecting bus and no Redis/Telethon session."""
    instance = worker.TelethonWorker(cfg)
    instance.bus = FakeBus()
    instance._connected = True
    return instance


def _envelope(command: dict[str, Any]) -> dict[str, Any]:
    """Wrap a command the way ``redis.asyncio.pubsub.listen()`` delivers it."""
    return {
        "type": "message",
        "channel": CHANNEL_DELETE_OUT,
        "data": json.dumps(command, ensure_ascii=False),
    }


def _valid_command(**overrides: Any) -> dict[str, Any]:
    """The exact §1.4 payload."""
    command: dict[str, Any] = {
        "cmd_id": "0b1e2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "message_id": 245,
        "chat_id": 7,
        "tg_chat_id": TG_CHAT_ID,
        "tg_msg_id": 57,
        "operator_username": "op1",
    }
    command.update(overrides)
    return command


# --------------------------------------------------------------------------- #
# A) Constant & wiring
# --------------------------------------------------------------------------- #


def test_channel_constant_matches_contract() -> None:
    """§1.4: the channel name is frozen."""
    assert worker.CHANNEL_DELETE_OUT == CHANNEL_DELETE_OUT


def test_delete_channel_is_not_a_worker_output_channel() -> None:
    """§8.10: ``tg_delete_out`` is a command channel, not something we emit."""
    if hasattr(worker, "WORKER_CHANNELS"):
        assert CHANNEL_DELETE_OUT not in worker.WORKER_CHANNELS


def test_stats_has_delete_out_counter() -> None:
    """§1.6: the deletion counter lives on ``_PublishStats``."""
    stats = worker._PublishStats()
    assert stats.delete_out == 0
    # Every pre-existing counter must survive untouched.
    for legacy in ("incoming", "deleted", "edited", "acks", "failures",
                   "reactions", "forwarded"):
        assert getattr(stats, legacy) == 0


def test_handler_exists_on_worker() -> None:
    assert callable(getattr(worker.TelethonWorker, "_handle_delete_command", None))


# --------------------------------------------------------------------------- #
# B) Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_valid_payload_calls_delete_messages_request(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._handle_delete_command(_envelope(_valid_command()))

    requests = client.delete_requests()
    assert len(requests) == 1
    request = requests[0]
    # The ``channels`` flavour takes the chat as ``channel`` and a *list* id.
    assert request.channel == FAKE_PEER
    assert request.id == [57]


@pytest.mark.asyncio
async def test_valid_payload_publishes_contract_ack(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient()

    await instance._handle_delete_command(_envelope(_valid_command()))

    acks = instance.bus.acks()
    assert len(acks) == 1
    ack = acks[0]
    assert ack["cmd_id"] == "0b1e2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    assert ack["cmd_kind"] == "DELETE"
    assert ack["message_id"] == 245
    assert ack["tg_msg_id"] == 57
    assert ack["ok"] is True
    assert ack["error"] is None


@pytest.mark.asyncio
async def test_valid_delete_increments_statistics(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient()

    await instance._handle_delete_command(_envelope(_valid_command()))

    assert instance.bus.stats.delete_out == 1
    # A success is a regular ack, not a failure.
    assert instance.bus.stats.acks == 1
    assert instance.bus.stats.failures == 0


@pytest.mark.asyncio
async def test_numeric_string_ids_are_coerced(worker_config: Any) -> None:
    """Backend sends JSON ints, but a stringified id must not break the RPC."""
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._handle_delete_command(
        _envelope(_valid_command(tg_msg_id="57", tg_chat_id=str(TG_CHAT_ID),
                                 chat_id="7", message_id="245"))
    )

    assert client.delete_requests()[0].id == [57]
    ack = instance.bus.acks()[0]
    assert ack["ok"] is True
    assert ack["message_id"] == 245


@pytest.mark.asyncio
async def test_peer_is_resolved_once_and_cached(worker_config: Any) -> None:
    """``_resolve_entity`` caches: a second delete must not re-resolve."""
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._handle_delete_command(_envelope(_valid_command()))
    await instance._handle_delete_command(_envelope(_valid_command(tg_msg_id=58)))

    assert client.entity_requests == [TG_CHAT_ID]
    assert [request.id for request in client.delete_requests()] == [[57], [58]]


@pytest.mark.asyncio
async def test_unresolvable_entity_falls_back_to_raw_id(worker_config: Any) -> None:
    """``get_entity`` failing keeps a usable peer (the raw id) instead of dying."""

    class LossyClient(RecordingClient):
        async def get_entity(self, chat_id: Any) -> Any:
            self.entity_requests.append(chat_id)
            raise ValueError("could not find the input entity")

    instance = _make_worker(worker_config)
    client = LossyClient()
    instance.client = client

    await instance._handle_delete_command(_envelope(_valid_command()))

    request = client.delete_requests()[0]
    assert request.channel == TG_CHAT_ID
    assert request.id == [57]
    assert instance.bus.acks()[0]["ok"] is True


# --------------------------------------------------------------------------- #
# C) Validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_tg_msg_id_is_negative_ack(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    command = _valid_command()
    del command["tg_msg_id"]
    await instance._handle_delete_command(_envelope(command))

    assert client.rpc_calls == []
    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert ack["cmd_kind"] == "DELETE"
    assert "tg_msg_id" in (ack["error"] or "")
    assert instance.bus.stats.delete_out == 0
    assert instance.bus.stats.failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["cmd_id", "tg_chat_id"])
async def test_missing_required_field_is_negative_ack(
    worker_config: Any, field: str
) -> None:
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    command = _valid_command()
    del command[field]
    await instance._handle_delete_command(_envelope(command))

    assert client.rpc_calls == []
    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert field in (ack["error"] or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["not-an-int", None, ""])
async def test_non_numeric_tg_msg_id_is_negative_ack(
    worker_config: Any, bad_value: Any
) -> None:
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._handle_delete_command(
        _envelope(_valid_command(tg_msg_id=bad_value))
    )

    assert client.rpc_calls == []
    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert "tg_msg_id" in (ack["error"] or "")


@pytest.mark.asyncio
async def test_invalid_json_is_dropped_without_ack(worker_config: Any) -> None:
    """A malformed frame has no ``message_id`` to ack — it is only logged."""
    instance = _make_worker(worker_config)
    instance.client = RecordingClient()

    await instance._handle_delete_command(
        {"type": "message", "channel": CHANNEL_DELETE_OUT, "data": "{not json"}
    )

    assert instance.bus.published == []


@pytest.mark.asyncio
async def test_pubsub_control_frames_are_ignored(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient()

    await instance._handle_delete_command(
        {"type": "subscribe", "channel": CHANNEL_DELETE_OUT, "data": 1}
    )

    assert instance.bus.published == []


@pytest.mark.asyncio
async def test_bytes_payload_is_decoded(worker_config: Any) -> None:
    """``decode_responses`` is off on the pub/sub connection: data arrives as bytes."""
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._handle_delete_command(
        {
            "type": "message",
            "channel": CHANNEL_DELETE_OUT.encode(),
            "data": json.dumps(_valid_command()).encode("utf-8"),
        }
    )

    assert client.delete_requests()[0].id == [57]
    assert instance.bus.acks()[0]["ok"] is True


@pytest.mark.asyncio
async def test_disconnected_client_is_negative_ack(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient(connected=False)

    await instance._handle_delete_command(_envelope(_valid_command()))

    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert "ConnectionError" in (ack["error"] or "")


@pytest.mark.asyncio
async def test_missing_client_is_negative_ack(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = None

    await instance._handle_delete_command(_envelope(_valid_command()))

    assert instance.bus.acks()[0]["ok"] is False


# --------------------------------------------------------------------------- #
# D) Telegram error handling (§1.6)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_forbidden_delete_is_success(worker_config: Any) -> None:
    """§1.6: already deleted / service message -> ``ok: true`` (goal reached)."""
    instance = _make_worker(worker_config)
    instance.client = RecordingClient(
        raise_exc=MessageDeleteForbiddenError(request=None)
    )

    await instance._handle_delete_command(_envelope(_valid_command()))

    acks = instance.bus.acks()
    assert len(acks) == 1
    assert acks[0]["ok"] is True
    assert acks[0]["error"] is None
    assert acks[0]["cmd_kind"] == "DELETE"
    assert instance.bus.stats.delete_out == 1
    assert instance.bus.stats.failures == 0


@pytest.mark.asyncio
async def test_flood_wait_is_negative_ack_with_seconds(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient(
        raise_exc=FloodWaitError(request=None, capture=42)
    )

    await instance._handle_delete_command(_envelope(_valid_command()))

    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert ack["cmd_kind"] == "DELETE"
    error = ack["error"] or ""
    assert "flood" in error.lower()
    assert "42" in error
    assert instance.bus.stats.delete_out == 0
    assert instance.bus.stats.failures == 1


@pytest.mark.asyncio
async def test_rpc_error_is_negative_ack(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient(raise_exc=MessageIdInvalidError(request=None))

    await instance._handle_delete_command(_envelope(_valid_command()))

    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert "MessageIdInvalidError" in (ack["error"] or "")
    assert instance.bus.stats.delete_out == 0


@pytest.mark.asyncio
async def test_timeout_is_negative_ack(
    worker_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "SEND_TIMEOUT_SECONDS", 0.01)

    class SlowClient(RecordingClient):
        async def __call__(self, request: Any) -> Any:
            import asyncio  # noqa: PLC0415

            await asyncio.sleep(30)
            return await super().__call__(request)  # pragma: no cover

    instance = _make_worker(worker_config)
    instance.client = SlowClient()

    await instance._handle_delete_command(_envelope(_valid_command()))

    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert "TimeoutError" in (ack["error"] or "")
    assert instance.bus.stats.delete_out == 0


@pytest.mark.asyncio
async def test_unexpected_error_is_negative_ack(worker_config: Any) -> None:
    """A non-RPC explosion must never escape the pump; it becomes a failed ack."""
    instance = _make_worker(worker_config)
    instance.client = RecordingClient(raise_exc=RuntimeError("client exploded"))

    await instance._handle_delete_command(_envelope(_valid_command()))

    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert "RuntimeError" in (ack["error"] or "")


# --------------------------------------------------------------------------- #
# E) Reaction acks carry ``cmd_kind`` (CONTRACTS_V5 §1.5)
# --------------------------------------------------------------------------- #


def _reaction_envelope(**overrides: Any) -> dict[str, Any]:
    command: dict[str, Any] = {
        "cmd_id": "reaction-1",
        "action": "SET",
        "message_id": 245,
        "chat_id": 7,
        "tg_chat_id": TG_CHAT_ID,
        "tg_msg_id": 57,
        "emoji": "👍",
        "operator_username": "op1",
    }
    command.update(overrides)
    return {
        "type": "message",
        "channel": worker.CHANNEL_REACTION_OUT,
        "data": json.dumps(command, ensure_ascii=False),
    }


@pytest.mark.asyncio
async def test_reaction_ack_carries_cmd_kind(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient()

    await instance._handle_reaction_command(_reaction_envelope())

    ack = instance.bus.acks()[0]
    assert ack["ok"] is True
    assert ack["cmd_kind"] == "REACTION"


@pytest.mark.asyncio
async def test_reaction_rejection_also_carries_cmd_kind(worker_config: Any) -> None:
    instance = _make_worker(worker_config)
    instance.client = RecordingClient()

    await instance._handle_reaction_command(_reaction_envelope(emoji="🦄"))

    ack = instance.bus.acks()[0]
    assert ack["ok"] is False
    assert ack["cmd_kind"] == "REACTION"


@pytest.mark.asyncio
async def test_plain_send_ack_keeps_legacy_shape(worker_config: Any) -> None:
    """A normal send must stay byte-identical: no ``cmd_kind`` key at all."""

    class SendClient(RecordingClient):
        async def send_message(self, **kwargs: Any) -> Any:
            return types.SimpleNamespace(id=1001)

    instance = _make_worker(worker_config)
    instance.client = SendClient()

    await instance._handle_outgoing_message(
        {
            "type": "message",
            "channel": worker.CHANNEL_OUTGOING,
            "data": json.dumps({
                "cmd_id": "send-1",
                "chat_id": 7,
                "tg_chat_id": TG_CHAT_ID,
                "message_id": 56,
                "text": "Просто текст",
            }, ensure_ascii=False),
        }
    )

    ack = instance.bus.acks()[0]
    assert ack["ok"] is True
    assert "cmd_kind" not in ack


# --------------------------------------------------------------------------- #
# F) Pump routing
# --------------------------------------------------------------------------- #


class ScriptedPubSub:
    """``pubsub.listen()`` stand-in yielding a fixed list of messages."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    async def listen(self) -> Any:
        for message in self._messages:
            yield message


@pytest.mark.asyncio
async def test_pump_routes_delete_channel(worker_config: Any) -> None:
    """§1.4: a ``tg_delete_out`` frame reaches ``_handle_delete_command``."""
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._pump_outgoing(ScriptedPubSub([_envelope(_valid_command())]))

    assert [request.id for request in client.delete_requests()] == [[57]]
    ack = instance.bus.acks()[0]
    assert ack["cmd_kind"] == "DELETE"
    assert ack["ok"] is True


@pytest.mark.asyncio
async def test_pump_does_not_misroute_delete_as_send(worker_config: Any) -> None:
    """A delete must never be parsed by the plain-send handler."""

    class SendClient(RecordingClient):
        async def send_message(self, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("tg_delete_out must not send a message")

        async def send_file(self, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("tg_delete_out must not send a file")

    instance = _make_worker(worker_config)
    instance.client = SendClient()

    await instance._pump_outgoing(ScriptedPubSub([_envelope(_valid_command())]))

    assert len(instance.bus.acks()) == 1
    assert instance.bus.acks()[0]["ok"] is True


@pytest.mark.asyncio
async def test_two_commands_are_acked_in_order(worker_config: Any) -> None:
    """The single pump keeps FIFO: ack #1 belongs to the first command."""
    instance = _make_worker(worker_config)
    client = RecordingClient()
    instance.client = client

    await instance._pump_outgoing(ScriptedPubSub([
        _envelope(_valid_command(cmd_id="first", tg_msg_id=11)),
        _envelope(_valid_command(cmd_id="second", tg_msg_id=12)),
    ]))

    acks = instance.bus.acks()
    assert [ack["cmd_id"] for ack in acks] == ["first", "second"]
    assert [request.id for request in client.delete_requests()] == [[11], [12]]
    assert instance.bus.stats.delete_out == 2
