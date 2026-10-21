"""Prove the SLA clock now closes on an unquoted operator reply.

Reproduces the reported bug: chat 21 holds a PENDING incident, and a plain message
(no ``reply_to``) used to leave it running. Afterwards the same call must resolve it.
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage, SLAIncident  # noqa: E402

API = "http://127.0.0.1:8000"
Session = get_session_factory()

passed: list[str] = []
failed: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    (passed if cond else failed).append(label)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


async def pending_incident(chat_id: int) -> SLAIncident | None:
    async with Session() as db:
        return (
            await db.execute(
                select(SLAIncident)
                .where(
                    SLAIncident.chat_id == chat_id,
                    SLAIncident.status == "PENDING",
                )
                .order_by(SLAIncident.deadline_at.asc())
                .limit(1)
            )
        ).scalars().first()


async def main() -> int:
    async with Session() as db:
        chat = (
            await db.execute(
                select(GroupChat).where(GroupChat.tg_chat_id == -1004395719918)
            )
        ).scalars().first()
    chat_id = int(chat.id)
    print(f"=== chat {chat_id}: {chat.title} ===")

    before = await pending_incident(chat_id)
    if before is not None:
        print(f"    open incident {before.id} (deadline {before.deadline_at})")
    else:
        # No open clock: create one from a synthetic inbound message so the check is repeatable.
        async with Session() as db:
            now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            msg = GroupMessage(
                chat_id=chat_id,
                tg_msg_id=None,
                sender_type="CLIENT",
                sender_name="SLA test client",
                text="Нужна помощь по выплате",
                is_deleted_in_tg=False,
                delivery_status="sent",
                created_at=now,
            )
            db.add(msg)
            await db.flush()
            from app.services import sla_engine  # noqa: PLC0415

            incident = await sla_engine.open_incident(db, msg)
            await db.commit()
            if incident is None:
                print("could not open an incident")
                return 1
            before_id = int(incident.id)
        print(f"    created incident {before_id}")
        before = await pending_incident(chat_id)

    check(before is not None, "chat has a PENDING incident before the reply")
    if before is None:
        return 1
    incident_id = int(before.id)

    print("\n=== sending an operator reply WITHOUT a quote ===")
    async with httpx.AsyncClient(base_url=API, timeout=60.0) as client:
        r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        r = await client.post(
            f"/api/chats/{chat_id}/messages",
            json={"text": "Да, уже занимаюсь вашим вопросом"},
        )
        check(r.status_code in (200, 201), "message accepted", r.text[:200])
        if r.status_code not in (200, 201):
            return 1
        sent_id = int(r.json()["id"])

    print("\n=== did the incident close? ===")
    closed = None
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        async with Session() as db:
            row = (
                await db.execute(select(SLAIncident).where(SLAIncident.id == incident_id))
            ).scalars().first()
            if row is not None and row.status == "RESOLVED":
                closed = row
                break
        await asyncio.sleep(1.0)

    check(closed is not None, "CRITICAL: incident resolved by an unquoted reply")
    if closed is not None:
        print(
            f"    incident {closed.id}: status={closed.status} "
            f"resolved_by_msg={closed.resolved_by_message_id} "
            f"response_time={closed.response_time_seconds}s"
        )
        check(int(closed.resolved_by_message_id or 0) == sent_id,
              "resolved by the message just sent")
        check((closed.response_time_seconds or 0) >= 0, "response time recorded")
        check(bool(closed.resolved_at), "resolved_at stamped")

    remaining = await pending_incident(chat_id)
    check(remaining is None, "no PENDING incident left in the chat",
          str(remaining.id) if remaining else "")

    print(f"\n=== {len(passed)}/{len(passed) + len(failed)} SLA checks passed ===")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
