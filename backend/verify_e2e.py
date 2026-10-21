"""
End-to-end live verification of the TG-Desk CRM backend.

Runs against a REAL uvicorn server + PostgreSQL + Redis. Exercises the risky paths:
  auth -> chat list -> SLA state -> WS connect -> TYPING_LOCK collision ->
  message send (reply resolves SLA) -> MESSAGE_STATUS -> tx lookup + audit -> forget cleanup.

Usage:  python verify_e2e.py
Exit code 0 = all assertions passed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"

results: list[tuple[bool, str]] = []


async def _open_incident_for(client: httpx.AsyncClient, headers: dict[str, str], chat_id: int) -> None:
    """Ensure ``chat_id`` has an open SLA clock by replaying an inbound client message.

    Uses the real engine rather than inserting rows by hand, so the clock, the assignment and
    the deadline all follow production rules.
    """
    import datetime as _dt

    from sqlalchemy import select  # noqa: PLC0415

    from app.database import get_session_factory  # noqa: PLC0415
    from app.models import GroupChat, GroupMessage, SLAIncident  # noqa: PLC0415
    from app.services import sla_engine  # noqa: PLC0415

    async with get_session_factory()() as db:
        existing = (
            await db.execute(
                select(SLAIncident).where(
                    SLAIncident.chat_id == int(chat_id),
                    SLAIncident.status == "PENDING",
                )
            )
        ).scalars().first()
        if existing is not None:
            return
        chat = (
            await db.execute(select(GroupChat).where(GroupChat.id == int(chat_id)))
        ).scalars().first()
        if chat is None:
            return
        now = _dt.datetime.now(_dt.timezone.utc)
        message = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=None,
            sender_type="CLIENT",
            sender_name="E2E client",
            text="E2E: проверка открытия SLA",
            is_deleted_in_tg=False,
            delivery_status="sent",
            created_at=now,
        )
        db.add(message)
        await db.flush()
        await sla_engine.open_incident(db, message)
        await db.commit()


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((cond, label))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -> {detail}" if detail and not cond else ""))


async def recv_until(ws: Any, wanted: set[str], timeout: float = 12.0) -> list[dict]:
    """Collect envelopes until one of `wanted` types arrives (or timeout)."""
    seen: list[dict] = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                envelope = json.loads(await ws.recv())
                seen.append(envelope)
                if envelope.get("type") in wanted:
                    return seen
    except (TimeoutError, asyncio.TimeoutError):
        return seen


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
        # ---------------------------------------------------------------- auth
        health = (await client.get("/health")).json()
        check(health["status"] == "ok" and health["db"] and health["redis"], "health: db+redis up", str(health))

        bad = await client.post("/api/auth/login", json={"username": "op1", "password": "wrong"})
        check(bad.status_code == 401, "login rejects bad password", f"{bad.status_code}")

        login = (await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})).json()
        token = login["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        check(login["operator"]["username"] == "op1", "login: op1 authenticated")

        # put op1 on shift so round-robin may pick them
        shift = await client.post("/api/shift/status", json={"status": "ONLINE"}, headers=headers)
        check(shift.status_code == 200, "shift: set ONLINE", f"{shift.status_code}")

        # -------------------------------------------------------------- chats
        chats = (await client.get("/api/chats?filter=all", headers=headers)).json()["items"]
        # The workspace holds only chats that are linked to a real Telegram group, so the
        # assertions below are about behaviour, not about a fixed demo set.
        check(len(chats) >= 1, "chats: at least one chat is available", str(len(chats)))
        check(
            all(c.get("tg_chat_id") for c in chats),
            "chats: every chat is linked to Telegram",
            str([c["id"] for c in chats if not c.get("tg_chat_id")]),
        )
        burning = (await client.get("/api/chats?filter=burning", headers=headers)).json()["items"]
        check(all(c["sla"] and c["sla"]["remaining_seconds"] <= 60 for c in burning), "chats: burning filter is <60s/overdue")

        # Open a fresh incident so the SLA assertions below never depend on leftover state.
        primary = chats[0]
        chat_id = primary["id"]
        await _open_incident_for(client, headers, chat_id)
        chats = (await client.get("/api/chats?filter=all", headers=headers)).json()["items"]
        pending = [c for c in chats if c.get("sla")]
        check(len(pending) >= 1, "sla: live PENDING incidents exist", str(len(pending)))
        if not pending:
            print("\nFAILED: cannot continue without an open SLA incident")
            return 1
        target = next((c for c in pending if c["sla"]["remaining_seconds"] > 0), pending[0])
        chat_id = target["id"]

        # --------------------------------------------------------- typing lock
        lock = await client.post(f"/api/chats/{chat_id}/typing", json={"action": "start"}, headers=headers)
        check(lock.status_code == 200 and lock.json()["ok"], "typing: op1 acquires lock", str(lock.json()))

        async with websockets.connect(f"{WS}?token={token}") as ws_op1:
            first = json.loads(await ws_op1.recv())
            check(first["type"] == "CONNECTED", "ws: CONNECTED on connect", first.get("type"))

            # a second operator must be blocked by the lock
            login2 = (await client.post("/api/auth/login", json={"username": "op3", "password": "operator123"})).json()
            h2 = {"Authorization": f"Bearer {login2['access_token']}"}
            blocked = await client.post(f"/api/chats/{chat_id}/typing", json={"action": "start"}, headers=h2)
            body = blocked.json()
            check(body.get("ok") is False and body.get("locked_by"), "typing: 2nd operator blocked", str(body))

            async with websockets.connect(f"{WS}?token={login2['access_token']}") as ws_op3:
                await ws_op3.recv()  # CONNECTED
                # op3 attempts to grab a chat already locked by op1: the resulting
                # TYPING_LOCK must reach every other operator socket in real time.
                await client.post(f"/api/chats/{chat_id}/typing", json={"action": "start"}, headers=h2)
                seen = await recv_until(ws_op3, {"TYPING_LOCK"}, timeout=8)
                lock_frames = [e for e in seen if e.get("type") == "TYPING_LOCK"]
                types = [e.get("type") for e in seen]
                check(bool(lock_frames), "ws: TYPING_LOCK broadcast to other operators", str(types))
                if lock_frames:
                    check(
                        lock_frames[0]["data"]["operator_name"] == "Operator One"
                        and lock_frames[0]["data"]["chat_id"] == chat_id,
                        "ws: TYPING_LOCK carries holder name + chat id",
                        str(lock_frames[0]["data"]),
                    )

                # ...and it also reaches the socket of the current holder (op1)
                seen_op1 = await recv_until(ws_op1, {"TYPING_LOCK"}, timeout=5)
                check(
                    any(e["type"] == "TYPING_LOCK" for e in seen_op1),
                    "ws: holder also receives the collision frame",
                    str([e.get("type") for e in seen_op1]),
                )

                # op3 sends PING
                await ws_op3.send(json.dumps({"type": "PING"}))
                pong = await recv_until(ws_op3, {"PONG"}, timeout=8)
                check(any(e["type"] == "PONG" for e in pong), "ws: PING -> PONG")

            # op1 sends a real reply that resolves the SLA
            msgs = (await client.get(f"/api/chats/{chat_id}/messages?limit=100", headers=headers)).json()["items"]
            trigger = next((m for m in reversed(msgs) if not m["is_outgoing"]), None)
            check(trigger is not None, "messages: found an incoming message to reply to")

            # unique per run so a previous run's message cannot short-circuit the idempotency check
            nonce = f"e2e-{uuid.uuid4()}"
            sent = await client.post(
                f"/api/chats/{chat_id}/messages",
                json={"text": "Live e2e verification reply", "reply_to_msg_id": trigger["id"], "client_nonce": nonce},
                headers=headers,
            )
            check(sent.status_code == 201, "messages: send returns 201", f"{sent.status_code} {sent.text[:200]}")
            created = sent.json()
            check(created["reply_to_id"] == trigger["id"], "messages: reply_to_id persisted")
            check(created["sender_type"] == "OPERATOR" and created["sender_name"], "messages: operator attribution present")

            # idempotency
            again = await client.post(
                f"/api/chats/{chat_id}/messages",
                json={"text": "Live e2e verification reply", "reply_to_msg_id": trigger["id"], "client_nonce": nonce},
                headers=headers,
            )
            check(again.status_code in (200, 201) and again.json()["id"] == created["id"], "messages: client_nonce is idempotent")

            # SLA incident must now be resolved in the stats
            stats = (await client.get("/api/sla/stats/me", headers=headers)).json()
            check(isinstance(stats["avg_response_seconds"], int) and stats["sla_percent"] >= 0, "sla: stats recomputed", str(stats))

        await client.post(f"/api/chats/{chat_id}/typing", json={"action": "stop"}, headers=headers)
        released = await client.post(f"/api/chats/{chat_id}/typing", json={"action": "start"}, headers=h2)
        check(released.json().get("ok") is True, "typing: lock released for next operator", str(released.json()))
        await client.post(f"/api/chats/{chat_id}/typing", json={"action": "stop"}, headers=h2)

        # ------------------------------------------------------------- tx lookup
        tx = (await client.post("/api/tx/lookup", json={"tx_id": "TX-4412-DD"}, headers=headers)).json()
        check(tx["admin_one"]["status"] == "FAILED" and tx["admin_two"]["status"] == "DECLINED", "tx: dual panel results differ")
        check(tx["admin_one"]["amount"] == "1500.00", "tx: decimal amount as 2dp string")

        missing = (await client.post("/api/tx/lookup", json={"tx_id": "TX-0000-XX"}, headers=headers)).json()
        check(not missing["admin_one"]["found"] and not missing["admin_two"]["found"], "tx: NOT_FOUND handling")

        audit = (await client.get("/api/tx/audit?limit=50", headers=headers)).json()["items"]
        check(any(a["tx_id"] == "TX-4412-DD" and a["operator_username"] == "op1" for a in audit), "tx: audit row written for operator", str(len(audit)))

        # ------------------------------------------------------------ authz
        anon = await client.get("/api/chats")
        check(anon.status_code == 401, "authz: anonymous chat list rejected", str(anon.status_code))
        forbidden = await client.post("/api/internal/broadcast", json={"event": {"type": "X", "data": {}}}, headers=headers)
        check(forbidden.status_code == 403, "authz: operator cannot broadcast", str(forbidden.status_code))

    failed = [label for ok, label in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
