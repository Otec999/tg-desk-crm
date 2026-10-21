"""
Live verification of the Telethon worker's Redis bridge.

Simulates exactly what telethon_worker/worker.py publishes and consumes so the
backend's Redis consumer, DB ingest, WS fan-out and outgoing->ack round-trip are
proven without real Telegram credentials:

  1. publish `tg_incoming`  -> backend must create the message + open an SLA
  2. publish `tg_deleted`   -> row must be soft-deleted (never removed)
  3. (backend) tg_outgoing  -> we act as the worker, reply on `tg_outgoing_ack`
  4. check worker:status / worker:heartbeat contract keys

Usage: python verify_worker_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis

REDIS_URL = "redis://localhost:6380/0"
BASE = "http://127.0.0.1:8000"

# Every run uses a fresh synthetic group and message id, so repeated runs never collide
# with rows left behind by an earlier run (which would mask the ingest assertions).
RUN = uuid.uuid4().int % 10_000_000
TG_CHAT_ID = -1000000000000 - RUN
TG_MSG_ID = 5_000_000 + RUN
TX = f"TX-{uuid.uuid4().hex[:4].upper()}-ZZ"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((cond, label))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not cond else ""))


async def main() -> int:
    rds = aioredis.from_url(REDIS_URL, decode_responses=True)
    await rds.ping()
    now_iso = datetime.now(UTC).isoformat()

    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
        login = (await client.post("/api/auth/login", json={"username": "op2", "password": "operator123"})).json()
        headers = {"Authorization": f"Bearer {login['access_token']}"}

        before = (await client.get("/api/chats?filter=all", headers=headers)).json()["items"]
        known_chats = len(before)

        # ---------------------------------------------------- 1. tg_incoming
        incoming = {
            "kind": "NEW_MESSAGE",
            "tg_chat_id": TG_CHAT_ID,
            "tg_msg_id": TG_MSG_ID,
            "chat_title": "Merchant Omega — Payouts",
            "chat_username": "merchant_omega_support",
            "sender_tg_id": 771234,
            "sender_name": "Jane Doe",
            "sender_username": "jane",
            "is_bot": False,
            "text": f"Hello, our payout {TX} is stuck, please check. Call me +1 555 0100",
            "reply_to_tg_msg_id": None,
            "tg_date": now_iso,
            "merchant_code": "MRC-OMEGA",
        }
        subscribers = await rds.publish("tg_incoming", json.dumps(incoming))
        check(subscribers >= 1, "redis: backend is subscribed to tg_incoming", f"subscribers={subscribers}")
        await asyncio.sleep(3.0)

        chats = (await client.get("/api/chats?filter=all", headers=headers)).json()["items"]
        new_chat = next((c for c in chats if c["tg_chat_id"] == TG_CHAT_ID), None)
        # Re-runs reuse the synthetic group created by a previous run, so accept either
        # "created now" or "already present" — both prove tg_chat_id resolution works.
        check(
            new_chat is not None or len(chats) == known_chats + 1,
            "ingest: unknown group auto-created",
            f"{known_chats} -> {len(chats)}",
        )
        check(new_chat is not None, "ingest: new chat resolvable by tg_chat_id")
        if not new_chat:
            return 1

        check(new_chat["unread_count"] >= 1, "ingest: unread counter incremented", str(new_chat["unread_count"]))
        check(new_chat["sla"] is not None, "ingest: SLA incident opened for client message")
        if new_chat["sla"]:
            remaining = new_chat["sla"]["remaining_seconds"]
            check(0 < remaining <= 300, "ingest: deadline is now + 5 min window", str(remaining))
        check(new_chat["assigned_operator_id"] is not None, "ingest: round-robin assigned to an ONLINE operator")

        msgs = (await client.get(f"/api/chats/{new_chat['id']}/messages", headers=headers)).json()["items"]
        created_msg = next((m for m in msgs if m["tg_msg_id"] == TG_MSG_ID), None)
        check(created_msg is not None, "ingest: message persisted with tg_msg_id")
        if created_msg:
            check(created_msg["sender_type"] == "CLIENT", "ingest: client classified as CLIENT")
            check(created_msg["phone"] is not None, "ingest: phone extracted from text", str(created_msg["phone"]))

        # -------------------------------------------------- 2. tg_deleted
        await rds.publish(
            "tg_deleted",
            json.dumps({"kind": "MESSAGE_DELETED", "tg_chat_id": TG_CHAT_ID, "tg_msg_ids": [TG_MSG_ID]}),
        )
        await asyncio.sleep(2.5)
        msgs = (await client.get(f"/api/chats/{new_chat['id']}/messages", headers=headers)).json()["items"]
        deleted = next((m for m in msgs if m["tg_msg_id"] == TG_MSG_ID), None)
        check(deleted is not None, "delete: row NOT removed from DB (anti-delete)")
        if deleted:
            check(deleted["is_deleted_in_tg"] is True, "delete: soft-delete flag set")
            check(bool(deleted["deleted_at"]), "delete: deleted_at timestamp recorded", str(deleted["deleted_at"]))
            check(bool(deleted["text"]), "delete: original text preserved for operators")

        # ------------------------- 3. outgoing -> ack round-trip (we are the worker)
        sent = await client.post(
            f"/api/chats/{new_chat['id']}/messages",
            json={"text": "Checking the gateway now, one moment.", "reply_to_msg_id": created_msg["id"] if created_msg else None},
            headers=headers,
        )
        check(sent.status_code == 201, "outgoing: operator message accepted", f"{sent.status_code}")
        created_out = sent.json()
        check(created_out["delivery_status"] == "pending", "outgoing: queued as pending", created_out["delivery_status"])

        pubsub = rds.pubsub()
        await pubsub.subscribe("tg_outgoing")
        await pubsub.get_message(timeout=1)  # skip subscribe confirmation
        # trigger again and capture the real payload the worker would receive
        sent2 = await client.post(
            f"/api/chats/{new_chat['id']}/messages",
            json={"text": "Second confirmation message."},
            headers=headers,
        )
        created_out2 = sent2.json()
        payload = None
        for _ in range(30):
            raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if raw and raw.get("type") == "message":
                payload = json.loads(raw["data"])
                break
        check(payload is not None, "outgoing: payload published to tg_outgoing")
        if payload:
            for key in ("cmd_id", "chat_id", "tg_chat_id", "text", "reply_to_tg_msg_id"):
                check(key in payload, f"outgoing: payload contains {key}", str(sorted(payload)))
            check(payload.get("message_id") == created_out2["id"], "outgoing: payload carries backend message id")

            # act as the worker: acknowledge with a real tg_msg_id
            ack = {
                "cmd_id": payload["cmd_id"],
                "chat_id": payload["chat_id"],
                "message_id": payload["message_id"],
                "tg_msg_id": 987654,
                "ok": True,
                "error": None,
            }
            await rds.publish("tg_outgoing_ack", json.dumps(ack))
            await asyncio.sleep(2.5)
            msgs = (await client.get(f"/api/chats/{new_chat['id']}/messages", headers=headers)).json()["items"]
            acked = next((m for m in msgs if m["id"] == payload["message_id"]), None)
            check(acked is not None and acked["delivery_status"] == "sent", "ack: message marked sent", str(acked["delivery_status"] if acked else None))
            check(acked is not None and acked["tg_msg_id"] == 987654, "ack: real tg_msg_id stored", str(acked["tg_msg_id"] if acked else None))

        await pubsub.close()

        # ------------------------------------------ 4. worker status contract
        await rds.hset("worker:status", mapping={"connected": "1", "updated_at": now_iso, "worker": "verify"})
        await rds.set("worker:heartbeat", now_iso, ex=60)
        info = (await client.get("/api/system/info", headers=headers)).json()
        check(isinstance(info.get("workers"), dict) and info["workers"].get("worker") == "verify", "status: /api/system/info reads worker:status", str(info.get("workers")))
        check(info.get("telegram_connected") is True, "status: telegram_connected reflects heartbeat", str(info.get("telegram_connected")))

    await rds.close()
    failed = [label for ok, label in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} worker-bridge checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


