"""Verify the CLEAR half of CONTRACTS_V4: removing a reaction in the CRM removes it
from Telegram too.

Runs as one live sequence with the worker left running, so the CLEAR command is
actually delivered. The Telegram side is read afterwards with the account session.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage  # noqa: E402

API = "http://127.0.0.1:8000"
TG_CHAT_ID = -1004338873193
REPO_ROOT = Path(r"C:\Users\Работа\Desktop\Custom CRM")
Session = get_session_factory()

passed: list[str] = []
failed: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    (passed if cond else failed).append(label)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


async def tail_ack(redis: Redis, cmd_id: str, timeout: float = 25.0) -> dict | None:
    """Wait for the ACK carrying ``cmd_id`` on ``tg_outgoing_ack``."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("tg_outgoing_ack")
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message:
                continue
            raw = message.get("data")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("cmd_id") == cmd_id:
                return payload
        return None
    finally:
        await pubsub.unsubscribe("tg_outgoing_ack")
        await pubsub.aclose()


async def telegram_reactions(tg_msg_id: int) -> list[str]:
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    env = load_env()
    client = TelegramClient(
        StringSession(env["TG_SESSION_STRING"]), int(env["TG_API_ID"]), env["TG_API_HASH"]
    )
    await client.connect()
    try:
        remote = await client.get_messages(TG_CHAT_ID, ids=tg_msg_id)
        payload = getattr(remote, "reactions", None) if remote else None
        results = getattr(payload, "results", None) if payload else None
        return [
            str(getattr(item.reaction, "emoticon", item.reaction)) for item in (results or [])
        ]
    finally:
        await client.disconnect()


async def main() -> int:
    redis: Redis = Redis.from_url(get_settings().redis_url)  # type: ignore[assignment]

    async with Session() as db:
        chat = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TG_CHAT_ID))
        ).scalars().first()
        message = (
            await db.execute(
                select(GroupMessage)
                .where(
                    GroupMessage.chat_id == chat.id,
                    GroupMessage.tg_msg_id.is_not(None),
                    GroupMessage.is_deleted_in_tg.is_(False),
                )
                .order_by(GroupMessage.id.desc())
                .limit(1)
            )
        ).scalars().first()

    msg_id = int(message.id)
    tg_msg_id = int(message.tg_msg_id)
    print(f"=== message {msg_id} (tg_msg_id={tg_msg_id}) ===")

    async with httpx.AsyncClient(base_url=API, timeout=60.0) as client:
        r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        # A fresh subscribe must exist *before* the request that publishes the command.
        pubsub = redis.pubsub()
        await pubsub.subscribe("tg_outgoing_ack")
        try:
            print("\n=== step 1: set 🔥 in the CRM ===")
            r = await client.put(f"/api/messages/{msg_id}/reaction", json={"emoji": "🔥"})
            check(r.status_code == 200, "PUT -> 200", r.text[:200])
            await asyncio.sleep(7)

            print("\n=== step 2: clear it in the CRM ===")
            r = await client.delete(f"/api/messages/{msg_id}/reaction")
            check(r.status_code == 200, "DELETE -> 200", r.text[:200])
            check(not r.json().get("items"), "CRM list is empty after clear", r.text[:160])

            # Drain the SET/CLEAR acks so a failure is visible immediately.
            deadline = time.monotonic() + 20.0
            acks: list[dict] = []
            while time.monotonic() < deadline:
                raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not raw:
                    if len(acks) >= 2:
                        break
                    continue
                data = raw.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                try:
                    payload = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if payload.get("message_id") == msg_id and payload.get("tg_msg_id") == tg_msg_id:
                    acks.append(payload)
        finally:
            await pubsub.unsubscribe("tg_outgoing_ack")
            await pubsub.aclose()

        check(len(acks) >= 2, "worker acknowledged both SET and CLEAR", f"{len(acks)} ack(s)")
        for ack in acks:
            check(ack.get("ok") is True, f"ack ok for cmd={str(ack.get('cmd_id'))[:8]}",
                  str(ack.get("error")))

    print("\n=== step 3: read the message back from Telegram ===")
    await asyncio.sleep(2)
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'worker.py' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        check=False,
    )
    await asyncio.sleep(7)
    emoji = await telegram_reactions(tg_msg_id)
    print(f"    Telegram reactions: {emoji}")
    check("🔥" not in emoji, "CRITICAL: CLEAR removed the reaction from Telegram", str(emoji))

    await redis.aclose()
    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} clear-flow checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
