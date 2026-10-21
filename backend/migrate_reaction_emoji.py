"""Migrate the reaction emoji set: ``😂`` -> ``👀``.

``ck_message_reactions_emoji_valid`` was generated from the ``ReactionEmoji`` enum, so
changing the enum in code does not change the constraint already stored in PostgreSQL.
Without this migration every ``👀`` reaction (and any row seeded with it) would be
rejected by the database.

The migration is idempotent: it drops and recreates the named constraint, and rewrites
the historical ``😂`` rows so no data violates the new rule.

Usage:  python migrate_reaction_emoji.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.database import engine
from app.enums import ReactionEmoji

CONSTRAINT = "ck_message_reactions_emoji_valid"
TABLE = "message_reactions"
OLD_EMOJI = "😂"
NEW_EMOJI = ReactionEmoji.EYES.value


def _values_clause() -> str:
    """Build the ``ARRAY[...]`` literal from the live enum."""
    quoted = ", ".join(f"'{value.value}'" for value in ReactionEmoji)
    return f"(emoji)::text = ANY ((ARRAY[{quoted}]::character varying)::text[])"


async def main() -> int:
    if engine.dialect.name != "postgresql":
        print(f"dialect is {engine.dialect.name}: the CHECK constraint is only enforced on PostgreSQL")
        return 0

    async with engine.begin() as conn:
        exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conrelid = to_regclass(:table) AND conname = :name"
                ),
                {"table": TABLE, "name": CONSTRAINT},
            )
        ).scalar_one_or_none()

        if exists:
            await conn.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT}"))
            print(f"dropped {CONSTRAINT}")

        # Rewrite historical rows before the new rule is enforced, otherwise the ADD
        # CONSTRAINT below would fail on the very data it is meant to allow.
        rewritten = await conn.execute(
            text(f"UPDATE {TABLE} SET emoji = :new WHERE emoji = :old"),
            {"new": NEW_EMOJI, "old": OLD_EMOJI},
        )
        print(f"rewrote {rewritten.rowcount} row(s) from {OLD_EMOJI} to {NEW_EMOJI}")

        await conn.execute(
            text(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK ({_values_clause()})")
        )
        print(f"added {CONSTRAINT}")

        current = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass(:table) AND conname = :name"
                ),
                {"table": TABLE, "name": CONSTRAINT},
            )
        ).scalar_one()
        print(f"\nnew definition:\n  {current}")

        if OLD_EMOJI in current or NEW_EMOJI not in current:
            print("\nFAILED: the constraint does not match the contract set")
            return 1

    print("\nOK: the database accepts the same six emoji as the application")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
