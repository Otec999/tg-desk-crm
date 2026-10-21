"""Verify CONTRACTS_V4 live: a CRM reaction reaches Telegram, then gets removed.

The whole sequence runs with the worker **running**: the Telegram session cannot be
opened concurrently (a second login steals the worker's updates), so the reaction is
confirmed by the worker's ACK on ``tg_outgoing_ack`` and the Telegram state is read
afterwards, once the worker has been stopped.
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


def stop_worker() -> None:
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


async def with_client(fn):
    """Run ``fn(client)`` with the account session (worker must be stopped)."""
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    env = load_env()
    client = TelegramClient(
        StringSession(env["TG_SESSION_STRING"]), int(env["TG_API_ID"]), env["TG_API_HASH"]
    )
    await client.connect()
    try:
        return await fn(client)
    finally:
        await client.disconnect()


async def main() -> int:
    redis: Redis = Redis.from_url(get_settings().redis_url)  # type: ignore[assignment]

    # The worker is expected to be running when this starts.
    async with Session() as db:
        chat = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TG_CHAT_ID))
        ).scalars().first()
        candidates = (
            await db.execute(
                select(GroupMessage)
                .where(
                    GroupMessage.chat_id == chat.id,
                    GroupMessage.tg_msg_id.is_not(None),
                    GroupMessage.is_deleted_in_tg.is_(False),
                    GroupMessage.delivery_status == "sent",
                )
                .order_by(GroupMessage.id.desc())
                .limit(40)
            )
        ).scalars().all()

    print("=== step 0: find a message Telegram actually holds ===")
    stop_worker()
    await asyncio.sleep(7)

    async def find(client):
        for candidate in candidates:
            remote = await client.get_messages(TG_CHAT_ID, ids=int(candidate.tg_msg_id))
            if remote is not None:
                return candidate
        return None

    message = await with_client(find)
    check(message is not None, "a CRM message exists in Telegram")
    if message is None:
        await redis.aclose()
        return 1

    msg_id = int(message.id)
    tg_msg_id = int(message.tg_msg_id)
    print(f"    using message {msg_id} (tg_msg_id={tg_msg_id})")

    # The worker must be alive for the command to be consumed.
    print("\n=== step 1: start the worker, then place 👀 in the CRM ===")
    subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "telethon_worker" / "worker.py"), "--log-level", "INFO"],
        cwd=str(REPO_ROOT / "telethon_worker"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(22)

    pubsub = redis.pubsub()
    await pubsub.subscribe("tg_outgoing_ack")

    try:
        async with httpx.AsyncClient(base_url=API, timeout=60.0) as http:
            r = await http.post(
                "/api/auth/login", json={"username": "op1", "password": "operator123"}
            )
            http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

            r = await http.put(f"/api/messages/{msg_id}/reaction", json={"emoji": "👀"})
            check(r.status_code == 200, "PUT /reaction -> 200", r.text[:180])
            check(
                any(i.get("emoji") == "👀" for i in r.json().get("items", [])),
                "CRM stores 👀",
            )

            ack = await wait_ack(pubsub, tg_msg_id, action_cutoff=25.0)
            check(ack is not None, "worker acknowledged the SET", str(ack))
            if ack:
                check(ack.get("ok") is True, "SET ACK ok", str(ack.get("error")))

            print("\n=== step 2: clear the reaction in the CRM ===")
            r = await http.delete(f"/api/messages/{msg_id}/reaction")
            check(r.status_code == 200, "DELETE /reaction -> 200", r.text[:180])
            check(not r.json().get("items"), "CRM list empty after clear")

            ack2 = await wait_ack(pubsub, tg_msg_id, action_cutoff=25.0)
            check(ack2 is not None, "worker acknowledged the CLEAR", str(ack2))
            if ack2:
                check(ack2.get("ok") is True, "CLEAR ACK ok", str(ack2.get("error")))
    finally:
        await pubsub.unsubscribe("tg_outgoing_ack")
        await pubsub.aclose()

    print("\n=== step 3: confirm both states in Telegram ===")
    stop_worker()
    await asyncio.sleep(7)

    async def read(client):
        remote = await client.get_messages(TG_CHAT_ID, ids=tg_msg_id)
        payload = getattr(remote, "reactions", None) if remote else None
        results = getattr(payload, "results", None) if payload else None
        return [
            str(getattr(item.reaction, "emoticon", item.reaction)) for item in (results or [])
        ]

    final = await with_client(read)
    print(f"    Telegram reactions: {final}")
    # The CRM removed its reaction last, so Telegram must match that state.
    check("👀" not in final, "CRITICAL: CLEAR left no 👀 behind", str(final))

    await redis.aclose()
    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} live reaction checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


async def wait_ack(pubsub, tg_msg_id: int, *, action_cutoff: float) -> dict | None:
    """Read the next ACK for ``tg_msg_id``, or ``None`` when the window closes."""
    deadline = time.monotonic() + action_cutoff
    while time.monotonic() < deadline:
        raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if not raw:
            continue
        data = raw.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("tg_msg_id") == tg_msg_id and payload.get("message_id"):
            return payload
    return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
