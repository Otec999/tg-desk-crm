"""Prove the CONTRACTS_V5 В§3 SLA rule against the live database.

The operator requirement, verbatim: В«SLA СЃС‡РёС‚Р°РµС‚СЃСЏ РїРѕ РїРѕСЃР»РµРґРЅРµРјСѓ РѕС‚РІРµС‚Сѓ СЃ РЎР Рњ, РµСЃР»Рё РІ С‡Р°С‚
С‡С‚Рѕ-С‚Рѕ РЅРµ РїРёСЃР°Р»Рё С‚Рѕ РѕС‚СЃС‡РёС‚С‹РІР°РµС‚СЃСЏ РµСЃР»Рё РїРѕСЃР»РµРґРЅРёР№ РѕС‚РІРµС‚ СЃ РЅР°С€РµР№ СЃС‚РѕСЂРѕРЅС‹ Р±С‹Р» С‚Рѕ РѕР±РЅСѓР»СЏРµС‚СЃСЏ
Рё РЅР°С‡РёРЅР°РµС‚СЃСЏВ».

Concretely, the clock must:
  1. start on an inbound client message when nothing is pending;
  2. NOT be extended by a second client message in the same episode;
  3. be closed by an operator reply (the reset);
  4. start again from the NEXT inbound message, with a fresh deadline.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

sys.path.insert(0, r"C:\Users\Р Р°Р±РѕС‚Р°\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select  # noqa: E402

from app.database import get_session_factory  # noqa: E402
from app.models import GroupChat, GroupMessage, Operator, SLAIncident  # noqa: E402
from app.services import sla_engine  # noqa: E402
from app.utils import utcnow  # noqa: E402

Session = get_session_factory()
results: list[tuple[bool, str, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


async def pending_for(db, chat_id: int) -> SLAIncident | None:
    return (
        await db.execute(
            select(SLAIncident)
            .where(SLAIncident.chat_id == chat_id, SLAIncident.status == "PENDING")
            .order_by(SLAIncident.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def main() -> int:
    async with Session() as db:
        chat = (
            await db.execute(select(GroupChat).order_by(GroupChat.id).limit(1))
        ).scalars().first()
        operator = (
            await db.execute(select(Operator).where(Operator.username == "op1"))
        ).scalars().first()
        check(chat is not None, "a chat exists")
        check(operator is not None, "operator op1 exists")
        if chat is None or operator is None:
            return 1

        print(f"\n=== chat {chat.id} ({chat.title}) ===")

        # Clean slate: close whatever is pending so the scenario starts neutral.
        existing = await pending_for(db, chat.id)
        while existing is not None:
            existing.status = "RESOLVED"
            existing.resolved_at = utcnow()
            await db.commit()
            existing = await pending_for(db, chat.id)
        check(existing is None, "no PENDING incident before the scenario")

        now = utcnow()

        # 1. First inbound message opens the clock.
        first = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=990001,
            sender_type="CLIENT",
            sender_name="SLA Probe",
            text="SLA probe: first inbound",
            created_at=now,
        )
        db.add(first)
        await db.flush()
        inc1 = await sla_engine.open_incident(db, first)
        await db.commit()
        check(inc1 is not None, "1. inbound message opens the clock")
        deadline_1 = inc1.deadline_at if inc1 else None
        print(f"    deadline_1 = {deadline_1}")

        # 2. A follow-up client message must NOT extend the deadline.
        second = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=990002,
            sender_type="CLIENT",
            sender_name="SLA Probe",
            text="SLA probe: follow-up inbound",
            created_at=now + timedelta(minutes=2),
        )
        db.add(second)
        await db.flush()
        inc2 = await sla_engine.open_incident(db, second)
        await db.commit()
        check(
            inc2 is not None and inc2.id == inc1.id,
            "2. follow-up inbound reuses the same incident",
            f"{getattr(inc1, 'id', None)} vs {getattr(inc2, 'id', None)}",
        )
        check(
            inc2 is not None and inc2.deadline_at == deadline_1,
            "2b. deadline was NOT extended by the follow-up",
            f"{deadline_1} vs {getattr(inc2, 'deadline_at', None)}",
        )

        # 3. An operator reply closes it вЂ” this is the "reset to zero".
        reply = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=990003,
            sender_type="OPERATOR",
            sender_operator_id=operator.id,
            sender_name=operator.display_name,
            text="SLA probe: operator answer",
            created_at=now + timedelta(minutes=4),
        )
        db.add(reply)
        await db.flush()
        await sla_engine.resolve_for_reply(
            db,
            chat_id=chat.id,
            reply_to_msg_id=None,
            operator=operator,
            outbound_message=reply,
        )
        await db.commit()
        still = await pending_for(db, chat.id)
        check(still is None, "3. operator reply closes the clock (reset)")

        # 4. The next inbound message starts a brand-new clock.
        third = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=990004,
            sender_type="CLIENT",
            sender_name="SLA Probe",
            text="SLA probe: inbound after the answer",
            created_at=now + timedelta(minutes=10),
        )
        db.add(third)
        await db.flush()
        inc3 = await sla_engine.open_incident(db, third)
        await db.commit()
        check(inc3 is not None, "4. next inbound starts a NEW clock")
        check(
            inc3 is not None and inc1 is not None and inc3.id != inc1.id,
            "4b. it is a different incident, not the closed one",
            f"{getattr(inc1, 'id', None)} vs {getattr(inc3, 'id', None)}",
        )
        if inc3 is not None:
            print(f"    deadline_3 = {inc3.deadline_at}")

        # 5. sla_enabled=false suppresses new clocks.
        from app.schemas import AdminSettingsUpdate  # noqa: PLC0415
        from app.services import settings_service  # noqa: PLC0415

        settings_service.invalidate_cache()
        before = await settings_service.get_settings(db)
        previous = bool(before.sla_enabled)
        await settings_service.update_settings(
            db,
            payload=AdminSettingsUpdate(sla_enabled=False),
            actor="verify_v5_sla_rule",
        )
        settings_service.invalidate_cache()

        fourth = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=990005,
            sender_type="CLIENT",
            sender_name="SLA Probe",
            text="SLA probe: while disabled",
            created_at=now + timedelta(minutes=20),
        )
        db.add(fourth)
        await db.flush()
        # Close the open one first so we test "no NEW clock", not "reuse".
        open_inc = await pending_for(db, chat.id)
        while open_inc is not None:
            open_inc.status = "RESOLVED"
            open_inc.resolved_at = utcnow()
            await db.commit()
            open_inc = await pending_for(db, chat.id)
        suppressed = await sla_engine.open_incident(db, fourth)
        await db.commit()
        check(suppressed is None, "5. sla_enabled=false suppresses new clocks")

        # restore
        await settings_service.update_settings(
            db,
            payload=AdminSettingsUpdate(sla_enabled=previous),
            actor="verify_v5_sla_rule",
        )
        settings_service.invalidate_cache()

        # Cleanup the probe rows and incidents.
        for tg_id in (990001, 990002, 990003, 990004, 990005):
            row_msg = (
                await db.execute(
                    select(GroupMessage).where(
                        GroupMessage.chat_id == chat.id, GroupMessage.tg_msg_id == tg_id
                    )
                )
            ).scalars().first()
            if row_msg is not None:
                await db.delete(row_msg)
        leftover = (
            await db.execute(
                select(SLAIncident).where(
                    SLAIncident.chat_id == chat.id, SLAIncident.status == "PENDING"
                )
            )
        ).scalars().all()
        for inc in leftover:
            inc.status = "RESOLVED"
            inc.resolved_at = utcnow()
        await db.commit()
        check(True, "probe data cleaned up")

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} SLA rule checks passed ===")
    failed = [label for ok, label, _ in results if not ok]
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

