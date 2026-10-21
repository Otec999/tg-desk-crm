"""CONTRACTS_V3 end-to-end checks: forwarding + asymmetric edit history.

Runs against the live stack (FastAPI on :8000, Postgres, Redis, Telethon worker).

The checks that matter most:
  * a relayed message never leaks the original sender's name (``forwarded_from`` NULL);
  * a Telegram-side edit keeps the previous text in ``message_edits``;
  * a CRM-side edit does NOT create a history row, yet still reaches Telegram.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(r"C:\Users\Работа\Desktop\Custom CRM")
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage, MediaAttachment, MessageEdit  # noqa: E402
from app.utils import utcnow  # noqa: E402

AsyncSessionLocal = get_session_factory()

sys.stdout.reconfigure(encoding="utf-8")

API = "http://127.0.0.1:8000"
TG_CHAT_ID = -1004338873193

passed: list[str] = []
failed: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        passed.append(label)
        print(f"[PASS] {label}")
    else:
        failed.append(label)
        print(f"[FAIL] {label}" + (f"  -> {detail}" if detail else ""))


async def login() -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=API, timeout=60.0)
    resp = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
    resp.raise_for_status()
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def pick_chats() -> tuple[int, int]:
    """Source chat (the live Telegram one) and a target chat that is NOT it."""
    async with AsyncSessionLocal() as db:
        source = (
            await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == TG_CHAT_ID))
        ).scalar_one_or_none()
        if source is None:
            source = (
                await db.execute(select(GroupChat).order_by(GroupChat.id).limit(1))
            ).scalar_one()
        target = (
            await db.execute(
                select(GroupChat).where(GroupChat.id != source.id).order_by(GroupChat.id)
            )
        ).scalars().first()
        if target is None:
            raise SystemExit("need at least two chats to test forwarding")
        return int(source.id), int(target.id)


async def find_message_with_photo(chat_id: int) -> GroupMessage | None:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(GroupMessage)
                .join(MediaAttachment, MediaAttachment.message_id == GroupMessage.id)
                .where(GroupMessage.chat_id == chat_id, GroupMessage.is_deleted_in_tg.is_(False))
                .order_by(GroupMessage.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return None
        await db.refresh(row, ["attachments"])
        return row


async def _seed_inbound_message(chat_id: int, text: str) -> tuple[int, int] | None:
    """Insert a pristine inbound message and return ``(tg_msg_id, crm_id)``.

    The edit test needs a message whose whole edit history is known from the start.
    Reusing rows left behind by earlier runs made ``old_text`` ambiguous — once every
    inbound message had been edited the check could not run at all. A synthetic
    ``tg_msg_id`` keeps the row realistic for the ingest path without touching Telegram.
    """
    async with AsyncSessionLocal() as db:
        highest = (
            await db.execute(
                select(func.max(GroupMessage.tg_msg_id)).where(GroupMessage.chat_id == chat_id)
            )
        ).scalar()
        tg_msg_id = int(highest or 900000) + 1
        message = GroupMessage(
            chat_id=int(chat_id),
            tg_msg_id=tg_msg_id,
            sender_type="CLIENT",
            sender_name="History Test Client",
            text=text,
            is_deleted_in_tg=False,
            delivery_status="sent",
            created_at=utcnow(),
        )
        db.add(message)
        await db.commit()
        await db.refresh(message)
        return tg_msg_id, int(message.id)


async def main() -> int:
    client = await login()
    source_id, target_id = await pick_chats()
    print(f"source chat={source_id}  target chat={target_id}\n")

    # ---------------------------------------------------------------- 1. forward
    print("=== forwarding ===")
    photo_msg = await find_message_with_photo(source_id)
    if photo_msg is None:
        check(False, "found a message with a photo to forward")
    else:
        check(True, "found a message with a photo to forward")
        before_files = {a.storage_path for a in photo_msg.attachments}

        resp = await client.post(
            f"/api/messages/{photo_msg.id}/forward",
            json={"target_chat_id": target_id, "comment": "Пересылка из e2e-теста"},
        )
        check(resp.status_code == 201, "POST /forward -> 201", resp.text[:300])
        if resp.status_code == 201:
            forwarded = resp.json()
            check(
                forwarded.get("forwarded_from") is None,
                "CRITICAL: forwarded_from is NULL (sender name hidden)",
                str(forwarded.get("forwarded_from")),
            )
            check(
                forwarded.get("chat_id") == target_id,
                "relayed message landed in the target chat",
                str(forwarded.get("chat_id")),
            )
            check(
                "Пересылка из e2e-теста" in (forwarded.get("text") or ""),
                "comment kept in the relayed text",
                (forwarded.get("text") or "")[:80],
            )
            check(
                bool(forwarded.get("attachments")),
                "photo attachment copied to the relayed message",
                str(forwarded.get("attachments")),
            )

            # The relayed sender identity must be ours, not the original client's.
            check(
                forwarded.get("sender_type") == "OPERATOR",
                "sender_type is OPERATOR (sent as our account)",
                str(forwarded.get("sender_type")),
            )

            # Files must be physically copied, not shared.
            async with AsyncSessionLocal() as db:
                new_atts = (
                    await db.execute(
                        select(MediaAttachment).where(
                            MediaAttachment.message_id == forwarded["id"]
                        )
                    )
                ).scalars().all()
                new_paths = {a.storage_path for a in new_atts}
                check(
                    bool(new_paths) and new_paths.isdisjoint(before_files),
                    "CRITICAL: files physically copied (no shared paths)",
                    f"old={before_files} new={new_paths}",
                )
                for att in new_atts:
                    check(
                        not Path(att.storage_path).is_absolute(),
                        "storage_path stays relative to media_root",
                        att.storage_path,
                    )
                    absolute = Path(get_settings().media_root) / att.storage_path
                    check(
                        absolute.exists() and absolute.stat().st_size > 0,
                        "copied file exists on disk with content",
                        str(absolute),
                    )

    # Forwarding back into the same chat must be refused.
    same = await client.post(
        f"/api/messages/{photo_msg.id}/forward" if photo_msg else "/api/messages/1/forward",
        json={"target_chat_id": source_id},
    )
    check(same.status_code in (409, 422, 404), "forward into the same chat refused", str(same.status_code))

    # ------------------------------------------------------------- 2. CRM edit
    print("\n=== editing from CRM (no history) ===")
    async with AsyncSessionLocal() as db:
        client_msg = (
            await db.execute(
                select(GroupMessage)
                .where(
                    GroupMessage.chat_id == source_id,
                    GroupMessage.sender_type.in_(["CLIENT", "BOT"]),
                    GroupMessage.is_deleted_in_tg.is_(False),
                )
                .order_by(GroupMessage.id.desc())
                .limit(1)
            )
        ).scalars().first()

    if client_msg is None:
        check(False, "found an inbound message to edit from CRM")
    else:
        check(True, "found an inbound message to edit from CRM")
        async with AsyncSessionLocal() as db:
            before_edits = (
                await db.execute(
                    select(GroupMessage).where(GroupMessage.id == client_msg.id)
                )
            ).scalar_one()
            await db.refresh(before_edits, ["edits"])
            count_before = len(before_edits.edits)

        resp = await client.patch(
            f"/api/messages/{client_msg.id}", json={"text": "Правка оператора из CRM"}
        )
        check(resp.status_code == 200, "PATCH /messages/{id} -> 200", resp.text[:300])
        if resp.status_code == 200:
            body = resp.json()
            check(
                (body.get("message") or {}).get("text") == "Правка оператора из CRM",
                "text updated in the response",
                str((body.get("message") or {}).get("text")),
            )

            async with AsyncSessionLocal() as db:
                after = (
                    await db.execute(select(GroupMessage).where(GroupMessage.id == client_msg.id))
                ).scalar_one()
                await db.refresh(after, ["edits"])
                count_after = len(after.edits)
                check(
                    count_after == count_before,
                    "CRITICAL: CRM edit writes NO history row",
                    f"{count_before} -> {count_after}",
                )

    # ----------------------------------------------------- 3. Telegram-side edit
    print("\n=== editing from Telegram (history kept) ===")
    # The test creates its own inbound message instead of hunting for a fresh one:
    # reusing a row that earlier runs already edited made ``old_text`` ambiguous, and
    # a small database eventually runs out of unedited messages entirely.
    seeded = await _seed_inbound_message(source_id, "Базовая версия для проверки истории")
    if seeded is None:
        check(False, "could not seed an inbound message for the edit test")
    else:
        tg_msg_id, seeded_id = seeded
        check(True, "seeded a pristine inbound message")
        original_text = "Базовая версия для проверки истории"
        history_before = 0

        # Publish exactly what the worker would publish.
        settings = get_settings()
        redis: Redis = Redis.from_url(settings.redis_url)  # type: ignore[assignment]
        payload = {
            "kind": "MESSAGE_EDITED",
            "tg_chat_id": TG_CHAT_ID,
            "tg_msg_id": tg_msg_id,
            "text": "Клиент подменил текст через Telegram",
            "actor_name": "Jane Doe",
            "actor_username": "jane_doe",
        }
        await redis.publish("tg_edits", json.dumps(payload))
        await redis.aclose()
        await asyncio.sleep(3)

        async with AsyncSessionLocal() as db:
            after = (
                await db.execute(select(GroupMessage).where(GroupMessage.id == seeded_id))
            ).scalar_one()
            await db.refresh(after, ["edits"])
            check(
                after.text == "Клиент подменил текст через Telegram",
                "Telegram edit applied to the stored text",
                str(after.text),
            )
            telegram_edits = [e for e in after.edits if e.source == "TELEGRAM"]
            check(
                len(telegram_edits) > history_before,
                "CRITICAL: Telegram edit recorded a history row",
                f"{history_before} -> {len(telegram_edits)} rows",
            )
            if telegram_edits:
                newest = telegram_edits[-1]
                check(
                    newest.old_text == original_text,
                    "CRITICAL: history keeps the PRE-edit text",
                    f"expected {original_text[:40]!r}, got {newest.old_text[:40]!r}",
                )
                check(
                    newest.new_text == "Клиент подменил текст через Telegram",
                    "history stores the new text",
                )
                check(
                    newest.actor_name == "Jane Doe",
                    "history records the Telegram actor name",
                    str(newest.actor_name),
                )

        # The API must expose the history to every operator.
        resp = await client.get(f"/api/messages/{seeded_id}/edits")
        check(resp.status_code == 200, "GET /messages/{id}/edits -> 200", resp.text[:200])
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            check(len(items) >= 1, "history visible through the API", str(len(items)))
            check(
                any(i["source"] == "TELEGRAM" and i["old_text"] == original_text for i in items),
                "API history carries the original text",
            )

    # ------------------------------------------------------- 4. rejection rules
    print("\n=== guards ===")
    resp = await client.patch("/api/messages/99999999", json={"text": "нет такого"})
    check(resp.status_code == 404, "editing a missing message -> 404", str(resp.status_code))
    resp = await client.patch(
        f"/api/messages/{photo_msg.id}" if photo_msg else "/api/messages/1", json={"text": "   "}
    )
    check(resp.status_code == 422, "blank text rejected -> 422", str(resp.status_code))

    await client.aclose()

    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
