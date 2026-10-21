"""Verify the two additive schema fields reach the API the frontend consumes.

* §A ``MessageOut.deleted_source`` — must appear on timeline messages, so the UI can
  tell a CRM deletion from a Telegram one.
* §B ``OperatorOut.can_delete_messages`` / ``can_export`` — must appear in the login
  response (``LoginResponse.operator``), ``/api/auth/me`` and the shift roster.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def http(method: str, path: str, token: str | None = None, body=None):
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


def main() -> int:
    print("=" * 78)
    print("VERIFY: §A MessageOut.deleted_source и §B OperatorOut.can_delete_messages/can_export")
    print("=" * 78)

    status, login = http("POST", "/api/auth/login", body={"username": "op1", "password": "operator123"})
    record("login 200", status == 200, f"status={status}")
    if status != 200:
        return 1
    operator = login.get("operator", {})
    token = login["access_token"]

    print("\n--- §B OperatorOut в ответе логина ---")
    print(f"  operator keys: {sorted(operator.keys())}")
    record("login.operator.can_delete_messages есть", "can_delete_messages" in operator, str(operator.get("can_delete_messages")))
    record("login.operator.can_export есть", "can_export" in operator, str(operator.get("can_export")))
    record("тип bool", isinstance(operator.get("can_delete_messages"), bool) and isinstance(operator.get("can_export"), bool))

    status, me = http("GET", "/api/auth/me", token)
    record("/api/auth/me 200", status == 200, f"status={status}")
    if isinstance(me, dict):
        record("/api/auth/me.can_delete_messages", "can_delete_messages" in me, str(me.get("can_delete_messages")))
        record("/api/auth/me.can_export", "can_export" in me, str(me.get("can_export")))

    status, roster = http("GET", "/api/shift/operators", token)
    record("/api/shift/operators 200", status == 200, f"status={status}")
    items = roster.get("items", []) if isinstance(roster, dict) else []
    record(
        "каждый элемент ростера несёт флаги",
        bool(items) and all("can_delete_messages" in item and "can_export" in item for item in items),
        f"elements={len(items)}",
    )

    print("\n--- §A MessageOut.deleted_source в таймлайне ---")
    status, messages = http("GET", "/api/chats/7/messages?limit=200", token)
    record("GET messages 200", status == 200, f"status={status}")
    rows = messages.get("items", []) if isinstance(messages, dict) else []
    record("в каждом MessageOut есть ключ deleted_source", bool(rows) and all("deleted_source" in row for row in rows), f"messages={len(rows)}")

    deleted = [row for row in rows if row.get("is_deleted_in_tg")]
    live = [row for row in rows if not row.get("is_deleted_in_tg")]
    record(
        "у неудалённых deleted_source=None",
        all(row.get("deleted_source") is None for row in live),
        f"live={len(live)}",
    )
    record(
        "у всех удалённых deleted_source — допустимое значение",
        all(row.get("deleted_source") in {"TELEGRAM", "CRM"} for row in deleted),
        f"deleted={len(deleted)} values={sorted({row.get('deleted_source') for row in deleted})}",
    )
    for row in deleted[:3]:
        print(f"    id={row['id']} is_deleted_in_tg={row['is_deleted_in_tg']} deleted_source={row['deleted_source']!r} deleted_at={row.get('deleted_at')}")

    print("\n--- schema availability ---")
    from app.schemas import MessageOut, OperatorOut

    record("MessageOut.deleted_source в модели", "deleted_source" in MessageOut.model_fields)
    record(
        "OperatorOut.can_delete_messages/can_export в модели",
        {"can_delete_messages", "can_export"} <= set(OperatorOut.model_fields),
    )

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
    sys.exit(main())
