"""Prove that a message sent through the CRM really lands in Telegram.

Sends a reply into the real chat, waits for the worker's ACK, then reads the chat
with the account session to confirm the text is there.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage  # noqa: E402

API = "http://127.0.0.1:8000"
TG_CHAT_ID = -1004338873193
Session = get_session_factory()
REPO_ROOT = Path(r"C:\Users\Работа\Desktop\Custom CRM")

passed: list[str] = []
failed: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    (passed if cond else failed).append(label)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


async def main() -> int:
    unique = uuid.uuid4().hex[:8]
    text = f"Проверка отправки из CRM {unique}"

    async with Session() as db:
        chat = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TG_CHAT_ID))
        ).scalars().first()
    print(f"=== target: {chat.title} (id={chat.id}, tg_chat_id={chat.tg_chat_id}) ===")

    async with httpx.AsyncClient(base_url=API, timeout=120.0) as client:
        r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        r = await client.post(
            f"/api/chats/{chat.id}/messages",
            json={"text": text},
        )
        check(r.status_code in (200, 201), "POST /chats/{id}/messages -> 2xx", r.text[:250])
        if r.status_code not in (200, 201):
            return 1
        message_id = int(r.json()["id"])

    print("\n=== waiting for the worker ACK ===")
    delivered = None
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        async with Session() as db:
            row = (
                await db.execute(select(GroupMessage).where(GroupMessage.id == message_id))
            ).scalars().first()
            if row is not None and row.delivery_status in ("sent", "failed"):
                delivered = row
                print(f"    status={row.delivery_status} tg_msg_id={row.tg_msg_id}")
                break
        await asyncio.sleep(2.0)

    check(delivered is not None, "worker processed the message")
    if delivered is None:
        return 1
    check(delivered.delivery_status == "sent", "delivery_status == sent",
          str(delivered.delivery_status))
    check(delivered.tg_msg_id is not None, "Telegram message id stored",
          str(delivered.tg_msg_id))
    if delivered.tg_msg_id is None:
        return 1

    tg_id = int(delivered.tg_msg_id)

    print("\n=== reading the chat inside Telegram ===")
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
    await asyncio.sleep(6)

    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    env = load_env()
    client = TelegramClient(
        StringSession(env["TG_SESSION_STRING"]), int(env["TG_API_ID"]), env["TG_API_HASH"]
    )
    await client.connect()
    try:
        remote = await client.get_messages(TG_CHAT_ID, ids=tg_id)
        check(remote is not None, "message exists in the Telegram group")
        if remote is not None:
            print(f"    Telegram text: {remote.message!r}")
            check(remote.message == text, "CRITICAL: text matches what was typed in the CRM")
            me = await client.get_me()
            check(remote.sender_id == me.id, "sent from our account")
    finally:
        await client.disconnect()

    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} outbound checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
