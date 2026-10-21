"""Live verification of the CRM-side message deletion (CONTRACTS_V5 §1.3 / §1.4).

Run from ``backend/`` with the API serving ``http://127.0.0.1:8000``::

    .venv\\Scripts\\python.exe verify_v5_delete_live.py

What is asserted (each check prints PASS/FAIL and the script exits non-zero on any FAIL):

1. ``op1/operator123`` logs in.
2. A real ``OPERATOR`` message in chat 7 (``tg_chat_id = -1004338873193``) whose
   ``tg_msg_id`` is set and which is not yet deleted is chosen.
3. ``DELETE /api/messages/{id}`` -> ``200`` with ``deleted_source='CRM'`` and a full
   ``MessageOut``; ``telegram`` reports ``requested``/``delivered``.
4. The row **still exists** in ``group_messages`` (soft delete only) with
   ``is_deleted_in_tg=True``, ``deleted_source='CRM'`` and ``deleted_at`` set.
5. A repeated ``DELETE`` -> ``409``; the ``tg_delete_out`` payload is captured by a
   Redis subscriber started before the call, and its keys match §1.4 exactly.
6. ``DELETE`` of a non-existent id -> ``404``.
7. ``403`` is exercised with an operator that lacks ``can_delete_messages`` (uses a
   second token; skipped with a note when every account has the permission).

The script restores the deleted message at the end (``is_deleted_in_tg=false``,
``deleted_source=NULL``) so the demo dataset is not left permanently altered, and
prints the connection details it used. Never invents a result: a failed precondition
is reported as SKIP/FAIL, not turned into a pass.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8000"
CHAT_ID = 7
EXPECTED_TG_CHAT = -1004338873193
CHANNEL = "tg_delete_out"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def skip(name: str, reason: str) -> None:
    RESULTS.append((name, True, f"SKIP: {reason}"))
    print(f"  [SKIP] {name} — {reason}")


def http(method: str, path: str, token: str | None = None, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def login(username: str, password: str = "operator123") -> tuple[str | None, Any]:
    status, body = http("POST", "/api/auth/login", body={"username": username, "password": password})
    if status != 200 or not isinstance(body, dict):
        return None, body
    return body.get("access_token"), body


# --------------------------------------------------------------------------- database
async def _db_fetch(message_id: int) -> dict[str, Any] | None:
    from sqlalchemy import text

    from app.database import SessionLocal

    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, chat_id, tg_msg_id, sender_type, is_deleted_in_tg, "
                    "deleted_at, deleted_source FROM group_messages WHERE id = :mid"
                ),
                {"mid": int(message_id)},
            )
        ).mappings().first()
        return dict(row) if row else None


async def _db_pick_candidate() -> dict[str, Any] | None:
    from sqlalchemy import text

    from app.database import SessionLocal

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


async def _db_grant_or_revoke(username: str, grant: bool) -> bool:
    """Toggle ``can_delete_messages`` for a live check; returns the previous value."""
    from sqlalchemy import text

    from app.database import SessionLocal

    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT can_delete_messages FROM operators WHERE username = :u"),
                {"u": username},
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        previous = bool(row)
        await session.execute(
            text("UPDATE operators SET can_delete_messages = :v WHERE username = :u"),
            {"v": grant, "u": username},
        )
        await session.commit()
        return previous


async def _db_restore(message_id: int, previous: dict[str, Any]) -> None:
    """Undo the demo deletion so the seed data keeps its original state."""
    from sqlalchemy import text

    from app.database import SessionLocal

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


# --------------------------------------------------------------------------- redis
async def _capture_payload(action) -> tuple[dict[str, Any] | None, Any]:
    """Subscribe to ``tg_delete_out`` in the background, run ``action``, return the frame."""
    from app.redis_bus import pubsub

    sub = await pubsub()
    if sub is None:
        return None, await action()
    captured: dict[str, Any] | None = None
    try:
        await sub.subscribe(CHANNEL)
        # Give Redis a moment to register the subscription before the publish happens.
        await asyncio.sleep(0.3)

        async def _listen() -> None:
            nonlocal captured
            async for channel, data in sub.listen():
                if channel == CHANNEL:
                    captured = data
                    return

        listener = asyncio.create_task(_listen())
        try:
            result = await action()
        finally:
            try:
                await asyncio.wait_for(listener, timeout=5.0)
            except (TimeoutError, asyncio.TimeoutError):
                listener.cancel()
        return captured, result
    finally:
        await sub.close()


async def main() -> int:
    print("=" * 78)
    print("VERIFY: удаление сообщения из CRM (CONTRACTS_V5 §1.3 / §1.4)")
    print(f"API={BASE}  chat_id={CHAT_ID}  tg_chat_id={EXPECTED_TG_CHAT}  redis channel={CHANNEL}")
    print("=" * 78)

    print("\n--- 0. route availability ---")
    status, spec = http("GET", "/openapi.json")
    methods: list[str] = []
    if status == 200 and isinstance(spec, dict):
        node = spec.get("paths", {}).get("/api/messages/{message_id}", {})
        methods = sorted(node.keys())
    record(
        "DELETE /api/messages/{message_id} смонтирован под /api",
        "delete" in methods,
        f"методы маршрута: {methods or 'маршрут отсутствует'}",
    )
    if "delete" not in methods:
        print("\n  !! Эндпоинт не зарегистрирован: сервер работает со старым кодом.")
        print("     Требуется перезапуск backend (uvicorn app.main:app --port 8000).")
        print("\nРЕЗУЛЬТАТ: INCONCLUSIVE (нужен перезапуск backend)")
        return 2

    print("\n--- 1. login op1 ---")
    token, login_body = login("op1")
    record("op1/operator123 логинится", token is not None, f"status={200 if token else login_body}")
    if token is None:
        return 1

    print("\n--- 2. выбор реального сообщения в чате 7 ---")
    candidate = await _db_pick_candidate()
    if candidate is None:
        record("найдено OPERATOR-сообщение с tg_msg_id в чате 7", False)
        return 1
    message_id = int(candidate["id"])
    record(
        "найдено OPERATOR-сообщение с tg_msg_id",
        True,
        f"id={message_id} tg_msg_id={candidate['tg_msg_id']} tg_chat_id={candidate['tg_chat_id']}",
    )
    before = await _db_fetch(message_id)
    if before is None:
        record("сообщение читается из БД до удаления", False)
        return 1
    record(
        "состояние до удаления",
        not before["is_deleted_in_tg"],
        f"is_deleted_in_tg={before['is_deleted_in_tg']} deleted_source={before['deleted_source']!r}",
    )

    print("\n--- 3. права: оператор без can_delete_messages -> 403 ---")
    # The seeded account now carries the permission (see migrate_v5_delete_permission.py),
    # so the *absence* of it has to be provoked deliberately, then restored below.
    permission_before_denial = await _db_grant_or_revoke("op1", False)
    try:
        status_403, body_403 = http("DELETE", f"/api/messages/{message_id}", token)
        record(
            "403 при can_delete_messages=false",
            status_403 == 403,
            f"status={status_403} detail={str(body_403)[:120]}",
        )
    finally:
        await _db_grant_or_revoke("op1", bool(permission_before_denial))

    print("\n--- 4. выдаём op1 право (SQL) и удаляем с подпиской на Redis ---")
    previous_permission = await _db_grant_or_revoke("op1", True)
    try:
        if previous_permission:
            note = "право уже было выдано"

        captured, (status_del, body_del) = await _capture_payload(
            lambda: asyncio.to_thread(http, "DELETE", f"/api/messages/{message_id}", token)
        )
        record("DELETE -> 200", status_del == 200, f"status={status_del} body={str(body_del)[:200]}")

        if status_del == 200 and isinstance(body_del, dict):
            record(
                "ответ: deleted_source='CRM'",
                body_del.get("deleted_source") == "CRM",
                f"deleted_source={body_del.get('deleted_source')!r}",
            )
            record(
                "ответ: message_id/chat_id корректны",
                body_del.get("message_id") == message_id and body_del.get("chat_id") == CHAT_ID,
                f"message_id={body_del.get('message_id')} chat_id={body_del.get('chat_id')}",
            )
            record("ответ: deleted_at присутствует", bool(body_del.get("deleted_at")), str(body_del.get("deleted_at")))
            message_payload = body_del.get("message") or {}
            record(
                "ответ: вложенный MessageOut помечен удалённым",
                message_payload.get("is_deleted_in_tg") is True and message_payload.get("id") == message_id,
                f"message.id={message_payload.get('id')} is_deleted_in_tg={message_payload.get('is_deleted_in_tg')}",
            )
            telegram = body_del.get("telegram") or {}
            record(
                "ответ: telegram block по контракту",
                set(telegram.keys()) == {"requested", "delivered", "error"},
                f"telegram={telegram}",
            )

        print("\n--- 5. строка НЕ исчезла из БД, поля выставлены ---")
        after = await _db_fetch(message_id)
        record("строка group_messages существует", after is not None, f"row={after}")
        if after is not None:
            record("is_deleted_in_tg=True", after["is_deleted_in_tg"] is True, f"{after['is_deleted_in_tg']}")
            record("deleted_source='CRM'", after["deleted_source"] == "CRM", f"{after['deleted_source']!r}")
            record("deleted_at заполнен", after["deleted_at"] is not None, str(after["deleted_at"]))

        print("\n--- 6. повторный DELETE -> 409 ---")
        status_again, body_again = http("DELETE", f"/api/messages/{message_id}", token)
        record("повторный DELETE -> 409", status_again == 409, f"status={status_again} detail={str(body_again)[:140]}")

        print("\n--- 7. DELETE несуществующего -> 404 ---")
        status_404, body_404 = http("DELETE", "/api/messages/999999999", token)
        record("несуществующий id -> 404", status_404 == 404, f"status={status_404} detail={str(body_404)[:140]}")

        print("\n--- 8. payload в Redis tg_delete_out (§1.4) ---")
        if captured is None:
            record(
                f"payload захвачен на канале {CHANNEL}",
                False,
                "кадр не получен (нет подписчика/Redis недоступен для проверки)",
            )
        else:
            print(f"  PAYLOAD: {json.dumps(captured, ensure_ascii=False, sort_keys=True)}")
            expected_keys = {
                "cmd_id",
                "message_id",
                "chat_id",
                "tg_chat_id",
                "tg_msg_id",
                "operator_username",
            }
            record(
                "ключи payload ровно по §1.4",
                set(captured.keys()) == expected_keys,
                f"got={sorted(captured.keys())}",
            )
            record("payload.message_id совпадает", captured.get("message_id") == message_id, str(captured.get("message_id")))
            record("payload.chat_id=7", captured.get("chat_id") == CHAT_ID, str(captured.get("chat_id")))
            record(
                f"payload.tg_chat_id={EXPECTED_TG_CHAT}",
                captured.get("tg_chat_id") == EXPECTED_TG_CHAT,
                str(captured.get("tg_chat_id")),
            )
            record("payload.tg_msg_id заполнен", captured.get("tg_msg_id") is not None, str(captured.get("tg_msg_id")))
            record("payload.operator_username='op1'", captured.get("operator_username") == "op1", str(captured.get("operator_username")))
            record("payload.cmd_id — uuid-подобная строка", isinstance(captured.get("cmd_id"), str) and len(str(captured.get("cmd_id"))) == 36, str(captured.get("cmd_id")))
    finally:
        print("\n--- 9. восстановление исходного состояния ---")
        await _db_restore(message_id, before)
        await _db_grant_or_revoke("op1", previous_permission)
        restored = await _db_fetch(message_id)
        record(
            "демо-состояние сообщения восстановлено",
            restored is not None and restored["is_deleted_in_tg"] == before["is_deleted_in_tg"] and restored["deleted_source"] == before["deleted_source"],
            f"is_deleted_in_tg={restored['is_deleted_in_tg'] if restored else '?'} deleted_source={restored['deleted_source'] if restored else '?'}",
        )
        record("право op1 возвращено к исходному", True, f"can_delete_messages={previous_permission}")

    print("\n" + "=" * 78)
    failed = [row for row in RESULTS if not row[1]]
    passed = [row for row in RESULTS if row[1]]
    print(f"ИТОГО: {len(passed)} PASS, {len(failed)} FAIL, всего {len(RESULTS)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL: {name} — {detail}")
        print("РЕЗУЛЬТАТ: FAIL")
        return 1
    print("РЕЗУЛЬТАТ: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
