"""Live forwarding check that does not need the bot.

The bot token is currently rejected by Telegram (``Unauthorized``), so the source
message is taken from what the worker already ingested into the real chat. The
relayed message is then verified through the **account's own MTProto session**,
which is the only reliable reader: the Bot API is unavailable.

The worker is stopped for the read window, because a second login to the same
session would steal its update stream.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage, MediaAttachment, Operator  # noqa: E402

API = "http://127.0.0.1:8000"
SOURCE_TG = -1004338873193
TARGET_TG = -1004395719918
Session = get_session_factory()

REPO_ROOT = Path(r"C:\Users\Работа\Desktop\Custom CRM")


def load_env() -> dict[str, str]:
    """Read the repository ``.env`` the same way the worker does."""
    values: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return values
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values

passed: list[str] = []
failed: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    (passed if cond else failed).append(label)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


async def main() -> int:
    async with Session() as db:
        source_chat = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == SOURCE_TG))
        ).scalars().first()
        target = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TARGET_TG))
        ).scalars().first()
        operator = (
            await db.execute(select(Operator).where(Operator.username == "op1"))
        ).scalars().first()

        photo = (
            await db.execute(
                select(GroupMessage)
                .join(MediaAttachment, MediaAttachment.message_id == GroupMessage.id)
                .where(
                    GroupMessage.chat_id == source_chat.id,
                    GroupMessage.sender_type.in_(["CLIENT", "BOT"]),
                    GroupMessage.is_deleted_in_tg.is_(False),
                    MediaAttachment.kind == "PHOTO",
                )
                .order_by(GroupMessage.id.desc())
                .limit(1)
            )
        ).scalars().first()

    if photo is None:
        print("no inbound photo from Telegram is stored; nothing to relay")
        return 1

    print(f"=== source: CRM message {photo.id} in chat {source_chat.id} "
          f"(tg_msg_id={photo.tg_msg_id}) -> target chat {target.id} ===")

    async with httpx.AsyncClient(base_url=API, timeout=120.0) as client:
        r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        r = await client.post(
            f"/api/messages/{photo.id}/forward",
            json={"target_chat_id": int(target.id), "comment": "Гарантия пересылки, без имени"},
        )
        check(r.status_code == 201, "POST /forward -> 201", r.text[:300])
        if r.status_code != 201:
            return 1
        relayed = r.json()

    relayed_id = int(relayed["id"])
    check(relayed.get("forwarded_from") is None, "forwarded_from is NULL in the CRM row")
    check(bool(relayed.get("attachments")), "photo present in the forward response",
          str(relayed.get("attachments")))
    check(relayed.get("sender_type") == "OPERATOR", "sender_type=OPERATOR")

    print("\n=== waiting for the worker's Telegram ACK ===")
    tg_id = None
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        async with Session() as db:
            fresh = (
                await db.execute(select(GroupMessage).where(GroupMessage.id == relayed_id))
            ).scalars().first()
            if fresh is not None and fresh.tg_msg_id is not None:
                tg_id = int(fresh.tg_msg_id)
                print(f"    tg_msg_id={tg_id} status={fresh.delivery_status}")
                break
        await asyncio.sleep(2.0)

    check(tg_id is not None, "worker delivered the forward and acknowledged it")
    if tg_id is None:
        return 1

    print("\n=== verifying inside Telegram with the account session ===")
    print("NOTE: stopping the worker for this read window (same session).")
    import subprocess  # noqa: PLC0415

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
        message = await client.get_messages(TARGET_TG, ids=tg_id)
        check(message is not None, "relayed message exists in the target chat")
        if message is not None:
            me = await client.get_me()
            sender = message.sender_id
            check(sender == me.id, "author is OUR account, not the other company",
                  f"sender_id={sender} ours={me.id}")
            check(message.fwd_from is None,
                  "CRITICAL: Telegram shows no 'Forwarded from' header",
                  str(message.fwd_from))
            check(message.photo is not None, "photo arrived as a photo")
            text = message.message or ""
            check("Гарантия пересылки, без имени" in text, "our comment present", text[:90])
            print(f"    relayed text: {text!r}")
    finally:
        await client.disconnect()

    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} live checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
