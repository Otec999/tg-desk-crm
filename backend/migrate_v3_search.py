"""
Idempotent migration for CONTRACTS_V5 §6.1 — global message search indexes.

Why ``pg_trgm`` and not ``tsvector``
------------------------------------
The search in §6.2 is a **substring** search (invariant §8.8): it must find
``+1792560788`` by typing ``5607`` and find ``payout`` inside ``repayout``. Full-text
search (``to_tsvector``/``plainto_tsquery``) tokenises on word boundaries, so it can
never match the middle of a phone number or of a code. ``pg_trgm`` splits text into
overlapping 3-character trigrams and indexes them with GIN, which answers
``column ILIKE '%needle%'`` with an index scan instead of a sequential scan.

What this file creates
----------------------
1. ``CREATE EXTENSION IF NOT EXISTS pg_trgm`` — the trigram operator class and the
   ``similarity()`` function used for ranking both live in this extension.
2. Four GIN trigram indexes, all ``IF NOT EXISTS``:

   ====================  ==============  ==========================================
   index                 table           column
   ====================  ==============  ==========================================
   ``ix_gm_text_trgm``   group_messages  ``text``
   ``ix_gm_caption_trgm``group_messages  ``caption``
   ``ix_gm_sender_trgm`` group_messages  ``sender_name``
   ``ix_gc_title_trgm``  group_chats     ``title``
   ====================  ==============  ==========================================

   ``(text)`` is wrapped in parentheses exactly as §6.1 writes it, so the index
   expression is a *parenthesised column reference* — PostgreSQL stores it as a
   plain column index on the trigram opclass.

Non-PostgreSQL databases
------------------------
The automated test-suite runs on in-memory **SQLite**, which has neither ``pg_trgm``
nor GIN indexes. On any dialect other than ``postgresql`` this script prints a skip
notice and exits ``0`` instead of failing the pipeline.

Idempotency
-----------
Every statement is ``IF NOT EXISTS``; re-running the file is a no-op. Nothing is
dropped, no data is touched, no existing index is rebuilt.

Usage::

    python migrate_v3_search.py            # apply
    python migrate_v3_search.py --dry-run  # print the DDL that would run
"""

from __future__ import annotations

import asyncio
import sys
from typing import Final

from sqlalchemy import text

from app.database import engine

#: Extension required by both the indexes and the ``similarity()`` ranking.
EXTENSION_NAME: Final[str] = "pg_trgm"

#: ``(index name, table, indexed expression)`` — mirrors CONTRACTS_V5 §6.1 verbatim.
TRGM_INDEXES: Final[tuple[tuple[str, str, str], ...]] = (
    ("ix_gm_text_trgm", "group_messages", "text"),
    ("ix_gm_caption_trgm", "group_messages", "caption"),
    ("ix_gm_sender_trgm", "group_messages", "sender_name"),
    ("ix_gc_title_trgm", "group_chats", "title"),
)

#: Trigram GIN operator class (provided by ``pg_trgm``).
TRGM_OPS: Final[str] = "gin_trgm_ops"


def create_extension_sql() -> str:
    """``CREATE EXTENSION IF NOT EXISTS pg_trgm``."""
    return f"CREATE EXTENSION IF NOT EXISTS {EXTENSION_NAME}"


def create_index_sql(index_name: str, table: str, column: str) -> str:
    """Build the §6.1 ``CREATE INDEX IF NOT EXISTS`` statement for one column.

    The expression is emitted as ``((column) gin_trgm_ops)`` so the double
    parentheses unambiguously delimit the indexed expression, followed by the
    operator class.
    """
    return (
        f"CREATE INDEX IF NOT EXISTS {index_name} "
        f"ON {table} USING gin (({column}) {TRGM_OPS})"
    )


async def _dialect_name() -> str:
    """Return the driver dialect name (``postgresql``, ``sqlite``, ...)."""
    return engine.dialect.name.lower()


async def _existing_indexes(conn) -> set[str]:
    """Names of indexes already present in the current schema."""
    result = await conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    )
    return {str(row[0]) for row in result.fetchall()}


async def main(dry_run: bool = False) -> int:
    """Apply (or print) the pg_trgm extension and the four trigram indexes.

    Returns the process exit code: ``0`` on success *and* on the non-PostgreSQL skip,
    ``1`` when the database itself is unreachable.
    """
    dialect = await _dialect_name()
    if dialect != "postgresql":
        print(
            f"migrate_v3_search: skipped — dialect is '{dialect}', "
            "pg_trgm/GIN indexes exist on PostgreSQL only (SQLite test runs need no index)."
        )
        return 0

    extension_sql = create_extension_sql()
    index_statements = [create_index_sql(*spec) for spec in TRGM_INDEXES]

    if dry_run:
        print("[dry-run] would run on postgresql:")
        print(f"  {extension_sql}")
        for statement in index_statements:
            print(f"  {statement}")
        print(f"\n[dry-run] {len(index_statements)} index(es) + 1 extension checked")
        return 0

    try:
        async with engine.begin() as conn:
            await conn.execute(text(extension_sql))
            print(f"  ensured extension {EXTENSION_NAME}")

            before = await _existing_indexes(conn)
            created = 0
            for statement, (index_name, _table, _column) in zip(
                index_statements, TRGM_INDEXES, strict=True
            ):
                await conn.execute(text(statement))
                if index_name in before:
                    print(f"  index {index_name} already present")
                else:
                    print(f"  created index {index_name}")
                    created += 1

            final = await _existing_indexes(conn)
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero, never traceback-dump
        print(f"migrate_v3_search: FAILED — {type(exc).__name__}: {exc}")
        return 1

    print(f"\nmigrate_v3_search: {created} index(es) created (0 = schema already current)")
    for index_name, table, column in TRGM_INDEXES:
        state = "ok" if index_name in final else "MISSING"
        print(f"  [{state}] {index_name} on {table}({column})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(dry_run="--dry-run" in sys.argv)))
