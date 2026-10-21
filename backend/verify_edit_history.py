"""Verify the Telegram-edit history on a message that has NO edits yet.

``verify_v3.py`` reuses whatever message happens to be newest, so on repeated runs
it compares against a baseline that a previous run already overwrote. This script
deliberately picks a message with zero ``message_edits`` rows, guaranteeing the
"old_text == text before the edit" assertion is meaningful.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage, MessageEdit  # noqa: E402

TG_CHAT_ID = -1004338873193
Session = get_session_factory()

passed: list[str] = []
failed: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    (passed if cond else failed).append(label)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


async def pick_unedited() -> GroupMessage | None:
    """Newest inbound Telegram message that has never been edited."""
    async with Session() as db:
        chat = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TG_CHAT_ID))
        ).scalars().first()
        edited_ids = select(MessageEdit.message_id).distinct()
        return (
            await db.execute(
                select(GroupMessage)
                .where(
                    GroupMessage.chat_id == chat.id,
                    GroupMessage.sender_type == "CLIENT",
                    GroupMessage.tg_msg_id.is_not(None),
                    GroupMessage.is_deleted_in_tg.is_(False),
                    GroupMessage.id.not_in(edited_ids),
                )
                .order_by(GroupMessage.id.desc())
                .limit(1)
            )
        ).scalars().first()


async def main() -> int:
    redis: Redis = Redis.from_url(get_settings().redis_url)  # type: ignore[assignment]

    target = await pick_unedited()
    if target is None:
        print("no unedited inbound message is available to test with")
        return 1

    msg_id = int(target.id)
    tg_msg_id = int(target.tg_msg_id)
    baseline = target.text or ""
    print(f"=== message {msg_id} (tg_msg_id={tg_msg_id}) ===")
    print(f"    baseline text: {baseline!r}")

    async with Session() as db:
        before = (
            await db.execute(
                select(func.count()).select_from(MessageEdit).where(
                    MessageEdit.message_id == msg_id
                )
            )
        ).scalar_one()
    check(before == 0, "message starts with no edit history", str(before))

    new_text = f"Правка из Telegram {int(time.time())}"
    await redis.publish(
        "tg_edits",
        json.dumps(
            {
                "kind": "MESSAGE_EDITED",
                "tg_chat_id": TG_CHAT_ID,
                "tg_msg_id": tg_msg_id,
                "text": new_text,
                "actor_name": "Merchant User",
                "actor_username": "merchant_user",
            }
        ),
    )

    # Poll instead of sleeping a fixed amount: the backend may take a moment to consume.
    deadline = time.monotonic() + 30.0
    newest: MessageEdit | None = None
    while time.monotonic() < deadline:
        async with Session() as db:
            newest = (
                await db.execute(
                    select(MessageEdit)
                    .where(MessageEdit.message_id == msg_id)
                    .order_by(MessageEdit.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            if newest is not None:
                break
        await asyncio.sleep(1.0)

    check(newest is not None, "Telegram edit produced a history row")
    if newest is None:
        await redis.aclose()
        return 1

    print(f"    history: old={newest.old_text!r} -> new={newest.new_text!r}")
    check(newest.source == "TELEGRAM", "source is TELEGRAM", str(newest.source))
    check(newest.old_text == baseline, "CRITICAL: old_text equals the pre-edit text",
          f"expected {baseline!r}, got {newest.old_text!r}")
    check(newest.new_text == new_text, "new_text stores the edit")
    check(bool(newest.actor_name), "actor name recorded", str(newest.actor_name))

    async with Session() as db:
        live = (
            await db.execute(select(GroupMessage).where(GroupMessage.id == msg_id))
        ).scalars().first()
    check(live.text == new_text, "live message text updated", str(live.text))

    await redis.aclose()
    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} history checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
