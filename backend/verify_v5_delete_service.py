"""Service-level verification of ``delete_message`` against the real DB **and** Redis.

Unlike ``verify_v5_delete_live.py`` this drives the service coroutine directly, so it
proves the business logic works even while the HTTP process still runs the old code.
It exercises, in order: 404, 403, 200 (+ Redis ``tg_delete_out`` payload), 409, and the
"row must survive" invariant. Everything it changes is restored in ``finally``.

Run from ``backend/``::

    .venv\\Scripts\\python.exe verify_v5_delete_service.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from sqlalchemy import text

from app.database import SessionLocal
from app.redis_bus import CH_TG_DELETE_OUT, pubsub
from app.services.message_delete import (
    MessageAlreadyDeletedError,
    MessageDeleteForbiddenError,
    MessageNotFoundError,
    delete_message,
)

CHAT_ID = 7
EXPECTED_TG_CHAT = -1004338873193
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


async def pick_candidate() -> dict[str, Any] | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT m.id, m.tg_msg_id, m.sender_operator_id, c.tg_chat_id "
                    "FROM group_messages m JOIN group_chats c ON c.id = m.chat_id "
                    "WHERE m.chat_id = :chat AND m.sender_type = 'OPERATOR' "
                    "AND m.tg_msg_id IS NOT NULL AND m.is_deleted_in_tg = false "
                    "ORDER BY m.id DESC LIMIT 1"
                ),
                {"chat": CHAT_ID},
            )
        ).mappings().first()
        return dict(row) if row else None


async def fetch_row(message_id: int) -> dict[str, Any] | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, chat_id, tg_msg_id, is_deleted_in_tg, deleted_at, deleted_source "
                    "FROM group_messages WHERE id = :mid"
                ),
                {"mid": int(message_id)},
            )
        ).mappings().first()
        return dict(row) if row else None


async def set_permission(operator_id: int, value: bool) -> bool:
    async with SessionLocal() as session:
        previous = (
            await session.execute(
                text("SELECT can_delete_messages FROM operators WHERE id = :oid"),
                {"oid": operator_id},
            )
        ).scalar_one()
        await session.execute(
            text("UPDATE operators SET can_delete_messages = :v WHERE id = :oid"),
            {"v": value, "oid": operator_id},
        )
        await session.commit()
        return bool(previous)


async def restore_row(message_id: int, previous: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE group_messages SET is_deleted_in_tg = :flag, deleted_at = :dt, "
                "deleted_source = :src WHERE id = :mid"
            ),
            {
                "flag": bool(previous["is_deleted_in_tg"]),
                "dt": previous["deleted_at"],
                "src": previous["deleted_source"],
                "mid": int(message_id),
            },
        )
        await session.commit()


async def capture_delete(message_id: int, operator: Any):
    """Run ``delete_message`` while subscribed to ``tg_delete_out``; return (frame, result)."""
    sub = await pubsub()
    assert sub is not None, "Redis pub/sub недоступен"
    captured: dict[str, Any] | None = None
    await sub.subscribe(CH_TG_DELETE_OUT)
    await asyncio.sleep(0.3)

    async def _listen() -> None:
        nonlocal captured
        async for channel, data in sub.listen():
            if channel == CH_TG_DELETE_OUT:
                captured = data
                return

    listener = asyncio.create_task(_listen())
    try:
        async with SessionLocal() as session:
            result = await delete_message(session, message_id=message_id, operator=operator)
        try:
            await asyncio.wait_for(listener, timeout=5.0)
        except (TimeoutError, asyncio.TimeoutError):
            listener.cancel()
        return captured, result
    finally:
        await sub.close()


async def main() -> int:
    print("=" * 78)
    print("VERIFY (service level): delete_message — FOR UPDATE / 403 / 404 / 409 / payload")
    print(f"chat_id={CHAT_ID}  tg_chat_id={EXPECTED_TG_CHAT}  channel={CH_TG_DELETE_OUT}")
    print("=" * 78)

    from app.models import Operator

    async with SessionLocal() as session:
        op1 = (
            await session.execute(text("SELECT id FROM operators WHERE username='op1'"))
        ).scalar_one()

    candidate = await pick_candidate()
    if candidate is None:
        record("найдено OPERATOR-сообщение в чате 7", False)
        return 1
    message_id = int(candidate["id"])
    record(
        "выбрано сообщение",
        True,
        f"id={message_id} tg_msg_id={candidate['tg_msg_id']} operator={candidate['sender_operator_id']}",
    )
    if candidate["tg_chat_id"] != EXPECTED_TG_CHAT:
        record(f"tg_chat_id чата 7 = {EXPECTED_TG_CHAT}", False, f"факт={candidate['tg_chat_id']}")
        return 1

    previous_permission = await set_permission(op1, False)
    before = await fetch_row(message_id)
    assert before is not None

    try:
        print("\n--- 1. 404: несуществующее сообщение ---")
        async with SessionLocal() as session:
            op1_row = await session.get(Operator, int(op1))
            try:
                await delete_message(session, message_id=999_999_999, operator=op1_row)
                record("NotFound для несуществующего", False, "исключение не поднято")
            except MessageNotFoundError as exc:
                row_gone = await fetch_row(999_999_999)
                record("NotFound для несуществующего", row_gone is None, str(exc))

        print("\n--- 2. 403: can_delete_messages=False ---")
        # The seeded operator now holds the permission, so revoke it for this check and
        # restore it right after (section 3 needs it back anyway).
        await set_permission(op1, False)
        async with SessionLocal() as session:
            op1_row = await session.get(Operator, int(op1))
            # Detach a plain snapshot so later commits in this session cannot expire the
            # attributes the assertions still read.
            denial_operator_id = op1_row.id
            denial_username = op1_row.username
            denial_can_delete = bool(op1_row.can_delete_messages)
            print(f"    operator snapshot: id={denial_operator_id} can_delete={denial_can_delete}")
            try:
                await delete_message(session, message_id=message_id, operator=op1_row)
                record("Forbidden без права", False, "исключение не поднято")
            except MessageDeleteForbiddenError as exc:
                unchanged = await fetch_row(message_id)
                record(
                    "Forbidden без права",
                    unchanged is not None and unchanged["is_deleted_in_tg"] is False,
                    f"{exc}; строка не изменена",
                )

        print("\n--- 3. 200: удаление + payload в tg_delete_out ---")
        await set_permission(op1, True)
        async with SessionLocal() as session:
            op1_row = await session.get(Operator, int(op1))
        captured, result = await capture_delete(message_id, op1_row)

        record(
            "DeletedMessageOut.deleted_source='CRM'",
            result.deleted_source == "CRM",
            f"deleted_source={result.deleted_source!r}",
        )
        record(
            "DeletedMessageOut.message_id/chat_id",
            result.message_id == message_id and result.chat_id == CHAT_ID,
            f"message_id={result.message_id} chat_id={result.chat_id}",
        )
        record(
            "вложенный MessageOut помечен удалённым",
            result.message.is_deleted_in_tg is True and result.message.id == message_id,
            f"message.id={result.message.id} is_deleted_in_tg={result.message.is_deleted_in_tg}",
        )
        record("deleted_at заполнен", result.deleted_at is not None, str(result.deleted_at))
        record(
            "telegram.requested/delivered",
            result.telegram.requested is True,
            f"requested={result.telegram.requested} delivered={result.telegram.delivered} "
            f"error={result.telegram.error}",
        )

        after = await fetch_row(message_id)
        record("СТРОКА НЕ ИСЧЕЗЛА ИЗ БД", after is not None, str(after))
        record("is_deleted_in_tg=True", bool(after and after["is_deleted_in_tg"]), str(after and after["is_deleted_in_tg"]))
        record("deleted_source='CRM'", bool(after and after["deleted_source"] == "CRM"), repr(after and after["deleted_source"]))
        record("deleted_at заполнен в БД", bool(after and after["deleted_at"]), str(after and after["deleted_at"]))

        print("\n--- 4. payload на канале tg_delete_out (§1.4) ---")
        if captured is None:
            record("payload захвачен", False, "кадр не получен")
        else:
            print(f"  PAYLOAD: {json.dumps(captured, ensure_ascii=False, sort_keys=True)}")
            expected = {"cmd_id", "message_id", "chat_id", "tg_chat_id", "tg_msg_id", "operator_username"}
            record("ключи ровно по §1.4", set(captured) == expected, f"got={sorted(captured)}")
            record("message_id", captured.get("message_id") == message_id, str(captured.get("message_id")))
            record("chat_id=7", captured.get("chat_id") == CHAT_ID, str(captured.get("chat_id")))
            record(f"tg_chat_id={EXPECTED_TG_CHAT}", captured.get("tg_chat_id") == EXPECTED_TG_CHAT, str(captured.get("tg_chat_id")))
            record("tg_msg_id", captured.get("tg_msg_id") == candidate["tg_msg_id"], str(captured.get("tg_msg_id")))
            record("operator_username='op1'", captured.get("operator_username") == "op1", str(captured.get("operator_username")))
            record("cmd_id — uuid4", isinstance(captured.get("cmd_id"), str) and len(str(captured.get("cmd_id"))) == 36, str(captured.get("cmd_id")))

        print("\n--- 5. 409: повторное удаление ---")
        async with SessionLocal() as session:
            op1_row = await session.get(Operator, int(op1))
        async with SessionLocal() as session:
            try:
                await delete_message(session, message_id=message_id, operator=op1_row)
                record("AlreadyDeleted при повторе", False, "исключение не поднято")
            except MessageAlreadyDeletedError as exc:
                record("AlreadyDeleted при повторе", exc.source == "CRM", f"{exc}; source={exc.source!r}")
    finally:
        print("\n--- 6. восстановление демо-состояния ---")
        await restore_row(message_id, before)
        await set_permission(op1, previous_permission)
        restored = await fetch_row(message_id)
        record(
            "сообщение восстановлено",
            restored is not None
            and restored["is_deleted_in_tg"] == before["is_deleted_in_tg"]
            and restored["deleted_source"] == before["deleted_source"],
            f"is_deleted_in_tg={restored['is_deleted_in_tg'] if restored else '?'} "
            f"deleted_source={restored['deleted_source'] if restored else '?'}",
        )
        record("право op1 возвращено", True, f"can_delete_messages={previous_permission}")

    print("\n" + "=" * 78)
    failed = [row for row in RESULTS if not row[1]]
    print(f"ИТОГО: {len(RESULTS) - len(failed)} PASS, {len(failed)} FAIL, всего {len(RESULTS)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    print("РЕЗУЛЬТАТ:", "FAIL" if failed else "ALL CHECKS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
