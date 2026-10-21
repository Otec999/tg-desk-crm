"""Live check: does an edit made in the CRM reach Telegram?

Picks the message relayed by the previous run (it carries a known text), edits it
through the API, waits for the worker ACK, and boots the account session briefly
to read the text back from Telegram itself.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage, Operator  # noqa: E402

API = "http://127.0.0.1:8000"
TARGET_TG = -1004395719918
NEW_TEXT = "Текст исправлен оператором из CRM — проверка"
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
        target = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TARGET_TG))
        ).scalars().first()
        # The most recent operator message that already has a Telegram id.
        message = (
            await db.execute(
                select(GroupMessage)
                .where(
                    GroupMessage.chat_id == target.id,
                    GroupMessage.tg_msg_id.is_not(None),
                    GroupMessage.sender_type == "OPERATOR",
                    GroupMessage.is_deleted_in_tg.is_(False),
                )
                .order_by(GroupMessage.id.desc())
                .limit(1)
            )
        ).scalars().first()

    if message is None:
        print("no operator message with a Telegram id in the target chat")
        return 1

    msg_id = int(message.id)
    tg_id = int(message.tg_msg_id)
    print(f"=== editing CRM message {msg_id} (tg_msg_id={tg_id}) ===")
    print(f"    before: {(message.text or '')[:60]!r}")

    async with httpx.AsyncClient(base_url=API, timeout=120.0) as client:
        r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        r = await client.patch(f"/api/messages/{msg_id}", json={"text": NEW_TEXT})
        check(r.status_code == 200, "PATCH /messages/{id} -> 200", r.text[:250])
        if r.status_code != 200:
            return 1
        body = r.json()
        check(body.get("message", {}).get("text") == NEW_TEXT, "CRM text updated")
        check(bool(body.get("edited_in_telegram")), "response flags the Telegram mirror",
              str(body.get("edited_in_telegram")))

    print("\n=== waiting for the edit ACK ===")
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        async with Session() as db:
            fresh = (
                await db.execute(select(GroupMessage).where(GroupMessage.id == msg_id))
            ).scalars().first()
            if fresh is not None and fresh.delivery_status in ("sent",):
                print(f"    delivery_status={fresh.delivery_status}")
                break
        await asyncio.sleep(2.0)

    print("\n=== reading the text back from Telegram ===")
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
        remote = await client.get_messages(TARGET_TG, ids=tg_id)
        check(remote is not None, "message still exists in Telegram")
        if remote is not None:
            text = remote.message or ""
            print(f"    Telegram text: {text!r}")
            check(text == NEW_TEXT, "CRITICAL: Telegram shows the edit made in the CRM", text[:90])
            check(remote.fwd_from is None, "still no 'Forwarded from' header")
            check(remote.photo is not None, "photo survived the edit")
    finally:
        await client.disconnect()

    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} live edit checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
