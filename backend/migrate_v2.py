"""
Idempotent, additive schema migration for CONTRACTS_V2 and CONTRACTS_V3.

Changes (v3, added 2025-06-13 — CONTRACTS_V3 §2 «Редактирование сообщений»):

* ``Base.metadata`` now declares the new table ``message_edits``
  (:class:`app.models.MessageEdit`). It is a **new table**, and
  ``Base.metadata.create_all`` — called through :func:`app.database.init_models`
  below — creates missing tables automatically, so it needs **no** entry in
  :data:`COLUMNS` (that list is only for ``ALTER TABLE … ADD COLUMN``).
* No existing column changed, so nothing else is required: running this file on a
  database that already has the v2 schema simply adds ``message_edits`` (plus the
  supporting index ``ix_message_edits_message_created``) and reports 0 changes.
* Still no destructive statement: no table, column or row is ever dropped.

``Base.metadata.create_all`` creates missing *tables* but never adds *columns* to
tables that already exist. The live database predates this feature set, so the new
columns must be applied explicitly.

Every statement is guarded, so running this file repeatedly is a no-op. Nothing here
drops or rewrites data: it only widens the schema.

Usage:
    python migrate_v2.py            # apply
    python migrate_v2.py --dry-run  # print what would change
"""

from __future__ import annotations

import asyncio
import sys
from typing import Final

from sqlalchemy import text

from app.database import engine, init_models

#: (table, column, DDL fragment for ADD COLUMN)
#: CONTRACTS_V3 adds no column: its only schema object is the brand-new table
#: ``message_edits``, which :func:`init_models` (``create_all``) creates on its own.
COLUMNS: Final[list[tuple[str, str, str]]] = [
    # operators — cabinet, permissions, profile
    ("operators", "is_active", "BOOLEAN NOT NULL DEFAULT true"),
    ("operators", "max_chats", "INTEGER"),
    ("operators", "can_delete_messages", "BOOLEAN NOT NULL DEFAULT false"),
    ("operators", "can_export", "BOOLEAN NOT NULL DEFAULT false"),
    ("operators", "avatar_color", "VARCHAR(7)"),
    # group_messages — media metadata
    ("group_messages", "kind", "VARCHAR(16) NOT NULL DEFAULT 'TEXT'"),
    ("group_messages", "caption", "TEXT"),
    ("group_messages", "forwarded_from", "VARCHAR(255)"),
    # telegram_accounts — secrets widened to fit Fernet tokens
    ("telegram_accounts", "api_hash", "VARCHAR(512)"),
    ("telegram_accounts", "session_string", "TEXT"),
    # CONTRACTS_V2 §2.4: bots are replied to by @mention, so the username must persist.
    ("group_messages", "sender_username", "VARCHAR(128)"),
]

#: CHECK constraints the pre-V2 schema defined before the new enum values existed.
#: They must be redefined, otherwise promoting an operator to ADMIN is rejected.
CONSTRAINTS: Final[list[tuple[str, str, str]]] = [
    (
        "ck_operators_role_valid",
        "operators",
        "role IN ('OPERATOR', 'SUPERVISOR', 'ADMIN')",
    ),
    (
        "ck_admin_audit_log_action_valid",
        "admin_audit_log",
        "action IN ('create_operator', 'update_operator', 'deactivate_operator', "
        "'reset_password', 'create_account', 'update_account', 'delete_account', "
        "'admin_login', 'admin_login_failed', 'admin_logout')",
    ),
]


async def _table_columns(conn, table: str) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0] for row in result.fetchall()}


async def main(dry_run: bool = False) -> int:
    # ``init_models`` -> ``Base.metadata.create_all``: creates every missing table,
    # including ``message_edits`` (CONTRACTS_V3 §2.2). Columns are handled below.
    if dry_run:
        print("[dry-run] would call init_models() to create missing tables")
    else:
        await init_models()
        print("  ensured missing tables via create_all (incl. message_edits)")

    added = 0
    async with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            existing = await _table_columns(conn, table)
            if not existing:
                print(f"  skip {table}.{column}: table absent")
                continue
            if column in existing:
                continue

            statement = f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {ddl}'
            if dry_run:
                print(f"  [dry-run] {statement}")
            else:
                await conn.execute(text(statement))
                print(f"  added {table}.{column}")
            added += 1

        for name, table, expression in CONSTRAINTS:
            drop = f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'
            create = f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})'
            if dry_run:
                print(f"  [dry-run] redefine {name}")
            else:
                await conn.execute(text(drop))
                await conn.execute(text(create))
                print(f"  redefined constraint {name}")
            added += 1

    if dry_run:
        print(f"\n[dry-run] {added} change(s) would be applied")
    else:
        print(f"\nmigrate_v2: {added} change(s) applied (0 = schema already current)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(dry_run="--dry-run" in sys.argv)))

