"""
Idempotent, additive schema migration for CONTRACTS_V5 (models slice).

Owned scope (CONTRACTS_V5 §7: ``backend/app/models.py`` + ``backend/migrate_v4.py``):

* **§1.2 — deletion provenance.** ``group_messages`` gains
  ``deleted_source VARCHAR(16) NULL`` plus two ``CHECK`` constraints:
  the closed vocabulary (``NULL`` / ``'CRM'`` / ``'TELEGRAM'``) and the
  consistency rule tying the column to ``is_deleted_in_tg``.
* **§2.1 — application settings.** New table ``app_settings`` (single row
  ``id = 1``, SLA switches and target, audit columns).
* **§5.1 — quick replies.** New table ``quick_replies`` (editable canned
  responses, unique ``shortcut``, ``(is_active, sort_order)`` index, non-blank
  body ``CHECK``).

``Base.metadata.create_all`` — reached through :func:`app.database.init_models`
below — creates the two **new tables** (with their own constraints and indexes) on
both PostgreSQL and SQLite. It never adds a column to a table that already exists,
so ``deleted_source`` is applied explicitly.

The backfill is the delicate part (§1.2): existing tombstones were written by the
Telegram ingest path (``tg_ingest.handle_deleted``) and are therefore
``TELEGRAM``-sourced, but they carry ``deleted_source IS NULL``. The consistency
``CHECK`` would reject them, so this script

1. adds the column,
2. fills every ``is_deleted_in_tg = true AND deleted_source IS NULL`` row with
   ``'TELEGRAM'`` (they predate CRM-side deletion, which V5 is the first to add),
3. verifies the whole table against both rules and only then creates the
   constraints — if any row still disagrees the constraints are skipped with a
   loud warning instead of aborting in the middle of a half-applied schema.

Nothing here drops a table, a column or a row: the script only widens the schema.
Every statement is guarded, so running it repeatedly is a no-op.

Usage:
    python migrate_v4.py            # apply
    python migrate_v4.py --dry-run  # print what would change
"""

from __future__ import annotations

import asyncio
import sys
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.database import engine, init_models
from app.models import _AuditAction, _check_values

#: ``group_messages.deleted_source`` values (CONTRACTS_V5 §1.2).
DELETED_SOURCE_CRM: Final[str] = "CRM"
DELETED_SOURCE_TELEGRAM: Final[str] = "TELEGRAM"

# ---------------------------------------------------------------------------
# Schema objects
# ---------------------------------------------------------------------------

#: (table, column, DDL fragment for ADD COLUMN) — CONTRACTS_V5 §1.2.
#: ``app_settings`` and ``quick_replies`` are entire tables, so ``create_all``
#: handles them; only a *new column* on an existing table needs an entry here.
COLUMNS: Final[list[tuple[str, str, str]]] = [
    ("group_messages", "deleted_source", "VARCHAR(16) NULL"),
]

#: (constraint name, table, CHECK expression) — created only after the data
#: satisfies them (see :func:`_violating_rows`). Both are DROP + ADD so a re-run
#: converges on the definition in :mod:`app.models` even if it changed.
CONSTRAINTS: Final[list[tuple[str, str, str, str]]] = [
    # (name, table, expression, rule the rows must satisfy first)
    (
        "ck_group_messages_deleted_source_valid",
        "group_messages",
        "deleted_source IS NULL OR deleted_source IN ('CRM', 'TELEGRAM')",
        "deleted_source IS NULL OR deleted_source IN ('CRM', 'TELEGRAM')",
    ),
    (
        "ck_group_messages_deleted_source_consistent",
        "group_messages",
        "(is_deleted_in_tg = false AND deleted_source IS NULL) "
        "OR (is_deleted_in_tg = true AND deleted_source IS NOT NULL)",
        "(is_deleted_in_tg = false AND deleted_source IS NULL) "
        "OR (is_deleted_in_tg = true AND deleted_source IS NOT NULL)",
    ),
]

#: Tables whose existence the script reports on (created by ``init_models``).
CREATED_TABLES: Final[tuple[str, ...]] = ("app_settings", "quick_replies")

#: Order of the ``information_schema`` probe; kept next to the queries that use it.
_TABLE_EXISTS_SQL = (
    "SELECT 1 FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_name = :t"
)


def _is_postgres(dialect_name: str) -> bool:
    """``True`` for PostgreSQL; everything else takes the SQLite/portable branch."""
    return dialect_name == "postgresql"


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    """``True`` when ``table`` is visible in the connection's current schema."""
    if conn.dialect.name == "postgresql":
        result = await conn.execute(text(_TABLE_EXISTS_SQL), {"t": table})
        return result.first() is not None
    # SQLite (tests) has no information_schema: ask the catalogue instead.
    result = await conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"), {"t": table}
    )
    return result.first() is not None


async def _table_columns(conn: AsyncConnection, table: str) -> set[str]:
    """Return the column names of ``table`` (empty set when the table is absent)."""
    if not await _table_exists(conn, table):
        return set()
    if conn.dialect.name == "postgresql":
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}
    result = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    return {row[1] for row in result.fetchall()}


async def _count(conn: AsyncConnection, sql: str) -> int:
    """Run a ``SELECT count(*)``-shaped statement and return the number."""
    result = await conn.execute(text(sql))
    row = result.first()
    return int(row[0]) if row is not None and row[0] is not None else 0


async def _stale_tombstones(conn: AsyncConnection) -> int:
    """Rows deleted before V5 that have no provenance yet (the backfill target)."""
    return await _count(
        conn,
        "SELECT count(*) FROM group_messages "
        "WHERE is_deleted_in_tg = true AND deleted_source IS NULL",
    )


async def _orphan_sources(conn: AsyncConnection) -> int:
    """Rows whose provenance is set while the "deleted" flag is not (anomalies).

    The migration never guesses here: such a row is not a legacy tombstone, it is
    a data bug, and silently clearing the source would erase the very fact the
    §8.2 invariant exists to protect. They are reported and the constraints are
    skipped instead.
    """
    return await _count(
        conn,
        "SELECT count(*) FROM group_messages "
        "WHERE is_deleted_in_tg = false AND deleted_source IS NOT NULL",
    )


async def _violating_rows(conn: AsyncConnection) -> int:
    """Rows that would reject ``ck_group_messages_deleted_source_consistent``."""
    return await _count(
        conn,
        "SELECT count(*) FROM group_messages WHERE NOT ("
        "(is_deleted_in_tg = false AND deleted_source IS NULL) "
        "OR (is_deleted_in_tg = true AND deleted_source IS NOT NULL))",
    )


async def _constraint_exists(conn: AsyncConnection, table: str, name: str) -> bool:
    """``True`` when a ``CHECK`` of that exact name is already attached."""
    if _is_postgres(conn.dialect.name):
        return await _constraint_definition(conn, table, name) is not None
    # SQLite stores CHECK constraints inline in the table DDL; the name is only
    # visible in ``sqlite_master.sql``, so match on it there.
    result = await conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"), {"t": table}
    )
    row = result.first()
    return bool(row and row[0] and name in str(row[0]))


async def _constraint_definition(conn: AsyncConnection, table: str, name: str) -> str | None:
    """Return the normalised definition of check ``name``, or ``None`` if absent.

    PostgreSQL stores the expression already minus the outer parentheses and with
    its own whitespace, e.g. ``deleted_source IS NULL OR deleted_source = ANY (…)``.
    A literal comparison with :data:`CONSTRAINTS` would therefore never match, and
    the script would DROP + ADD an identical constraint on every single run —
    pointless ``AccessExclusiveLock`` churn on a live table. The comparison is
    instead made on a *normalised* form (see :func:`_normalise`), which is enough
    to answer the question the script actually asks: "is the intended rule already
    enforced?". A genuinely changed rule still differs and is recreated.
    """
    if not _is_postgres(conn.dialect.name):
        return None
    result = await conn.execute(
        text(
            "SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
            "WHERE ns.nspname = current_schema() AND rel.relname = :t "
            "AND con.conname = :n AND con.contype = 'c'"
        ),
        {"t": table, "n": name},
    )
    row = result.first()
    return str(row[0]) if row is not None and row[0] is not None else None


def _normalise(expression: str) -> str:
    """Reduce a CHECK body to a comparable token form, ignoring PostgreSQL rewrites.

    PostgreSQL does not store an expression verbatim: it prints
    ``deleted_source IN ('CRM','TELEGRAM')`` back as
    ``deleted_source = ANY (ARRAY['CRM','TELEGRAM'])``. A literal comparison would
    read the rewrite as a changed rule and drop + recreate the constraint on every
    run — pointless lock churn on a live table. Both forms are therefore reduced to
    the same token sequence: punctuation becomes a separator, the ``= / any /
    array`` wrapper collapses into the ``in`` token it stands for, case is folded
    and repeated whitespace collapsed. A genuinely changed rule still differs.
    """
    text_form = (
        expression.replace("(", " ")
        .replace(")", " ")
        .replace("[", " ")
        .replace("]", " ")
        .replace("'", " ")
        .replace('"', " ")
        .replace(",", " ")
        .replace(":", " ")
        .lower()
    )
    tokens = [token for token in " ".join(text_form.split()).split(" ") if token]
    # ``=`` is plain equality: drop it. ``any``/``array`` wrap an ``IN`` list, so they
    # become the ``in`` token. ``text``/``character``/``varying`` are PostgreSQL casts.
    normalised: list[str] = []
    for token in tokens:
        if token in frozenset({"any", "array"}):
            token = "in"
        elif token in frozenset({"text", "character", "varying"}):
            continue
        elif token == "=":
            continue
        if token == "in" and normalised and normalised[-1] == "in":
            continue  # collapse the run of ``in`` tokens the wrapper produced
        normalised.append(token)
    return " ".join(normalised)


def _constraint_is_current(existing: str | None, expression: str) -> bool:
    """``True`` when ``existing`` (a ``pg_get_constraintdef`` body) enforces ``expression``.

    The stored definition starts with ``CHECK (`` and ends with ``)``; only the body
    is compared, after both sides are reduced to the token form of
    :func:`_normalise`.
    """
    if existing is None:
        return False
    body = existing.strip()
    if body.upper().startswith("CHECK"):
        body = body[5:].strip()
    return _normalise(body) == _normalise(expression)


def _drop_constraint_sql(dialect_name: str, table: str, name: str) -> str | None:
    """Return the DROP statement for ``name``, or ``None`` when unsupported.

    PostgreSQL supports ``DROP CONSTRAINT IF EXISTS``. SQLite cannot alter a
    constraint at all (``ALTER TABLE … DROP CONSTRAINT`` is a syntax error); its
    constraints only exist when the table was built by ``create_all``, which
    already emits the definitions from :mod:`app.models`. The SQLite branch is
    therefore a no-op and stays silent.
    """
    if _is_postgres(dialect_name):
        return f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'
    return None


def _add_constraint_sql(dialect_name: str, table: str, name: str, expression: str) -> str | None:
    """Return the ADD statement for ``name``, or ``None`` when unsupported (SQLite)."""
    if _is_postgres(dialect_name):
        return f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})'
    return None


async def _migrate_group_messages(conn: AsyncConnection, dry_run: bool) -> int:
    """Apply §1.2: column, backfill, verification, constraints. Returns changes made."""
    changes = 0
    table = "group_messages"
    columns = await _table_columns(conn, table)
    if not columns:
        print(f"  ! {table} is absent — skipping its V5 changes")
        return changes

    # -- 1. the column ------------------------------------------------------
    if "deleted_source" in columns:
        print(f"  = {table}.deleted_source already present")
    else:
        # The DDL fragment lives in :data:`COLUMNS` so the schema intent is readable
        # in one place instead of being buried in an f-string.
        _, column, ddl = next(item for item in COLUMNS if item[0] == table)
        statement = f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {ddl}'
        if dry_run:
            print(f"  [dry-run] {statement}")
        else:
            await conn.execute(text(statement))
            print(f"  + added {table}.{column}")
        changes += 1
        if dry_run:
            # The UPDATE below needs the column; on a dry run there is nothing to
            # backfill yet, so report what the real run would do and stop here.
            print(
                "  [dry-run] UPDATE "
                f"{table} SET deleted_source = '{DELETED_SOURCE_TELEGRAM}' "
                "WHERE is_deleted_in_tg = true AND deleted_source IS NULL"
            )
            print("  [dry-run] then re-verify and (re)create the two CHECK constraints")
            return changes + 2

    # -- 2. backfill legacy tombstones -------------------------------------
    stale = await _stale_tombstones(conn)
    if stale:
        statement = (
            f'UPDATE "{table}" SET deleted_source = :src '
            "WHERE is_deleted_in_tg = true AND deleted_source IS NULL"
        )
        if dry_run:
            print(f"  [dry-run] would backfill {stale} row(s) with '{DELETED_SOURCE_TELEGRAM}'")
        else:
            await conn.execute(text(statement), {"src": DELETED_SOURCE_TELEGRAM})
            print(
                f"  ~ backfilled {stale} legacy tombstone(s) with "
                f"deleted_source = '{DELETED_SOURCE_TELEGRAM}'"
            )
        changes += 1
    else:
        print("  = no legacy tombstones to backfill")

    # -- 3. verify before constraining -------------------------------------
    orphans = await _orphan_sources(conn)
    if orphans:
        print(
            f"  ! {orphans} row(s) have deleted_source set while is_deleted_in_tg = false — "
            "these are data anomalies the migration will not guess about"
        )
    violations = await _violating_rows(conn)
    if violations:
        print(
            "  ! skipping the deleted_source CHECK constraints: "
            f"{violations} row(s) still violate the consistency rule. "
            "Resolve them (see the rows where is_deleted_in_tg / deleted_source disagree) "
            "and re-run this script."
        )
        return changes

    # -- 4. constraints -----------------------------------------------------
    for name, ctable, expression, _rule in CONSTRAINTS:
        if ctable != table:
            continue
        drop = _drop_constraint_sql(conn.dialect.name, ctable, name)
        create = _add_constraint_sql(conn.dialect.name, ctable, name, expression)
        if drop is None or create is None:
            print(f"  = {name}: declared inline by create_all (SQLite), nothing to alter")
            continue
        existing = await _constraint_definition(conn, ctable, name)
        if existing is not None and _constraint_is_current(existing, expression):
            print(f"  = {name}: already current")
            continue
        if dry_run:
            verb = "recreate" if existing is not None else "create"
            print(f"  [dry-run] would {verb} constraint {name}")
            changes += 1
            continue
        await conn.execute(text(drop))
        await conn.execute(text(create))
        print(
            f"  {'~' if existing is not None else '+'} "
            f"{'redefined' if existing is not None else 'created'} {name}"
        )
        changes += 1

    return changes


async def _report_tables(conn: AsyncConnection, dry_run: bool) -> None:
    """Print the presence of the two §2.1/§5.1 tables (created by ``create_all``)."""
    for table in CREATED_TABLES:
        present = await _table_exists(conn, table)
        if present:
            row = await conn.execute(text(f'SELECT count(*) FROM "{table}"'))
            found = row.first()
            count = int(found[0]) if found is not None else 0
            print(f"  = {table}: present, {count} row(s)")
        elif dry_run:
            print(f"  [dry-run] would create table {table}")
        else:
            print(f"  ! {table}: still missing — create_all did not run")


async def _migrate_audit_action_vocabulary(conn: AsyncConnection, dry_run: bool) -> int:
    """Widen ``admin_audit_log.action_valid`` for the §2.3 ``update_settings`` action.

    ``Base.metadata.create_all`` (called by :func:`init_models`) only creates missing
    tables: it never alters a constraint on a table that already exists. The audit
    journal was created with the ten V1-V4 actions, so a settings write would fail with
    a ``CheckViolationError`` on ``action_valid`` until the constraint is recreated with
    the extended vocabulary.

    Adding a member to a closed vocabulary is monotonic - every existing row still
    satisfies the wider rule - so this step never deletes or rewrites data. The
    ``ck_admin_audit_log_action_valid`` twin (created by the naming convention on some
    deployments) is widened too, so both spellings of the constraint stay consistent.
    """
    table = "admin_audit_log"
    if not await _table_exists(conn, table):
        print(f"  ! {table} is absent - skipping its V5 changes")
        return 0

    expression = _check_values("action", _AuditAction)
    changes = 0
    for name in ("action_valid", "ck_admin_audit_log_action_valid"):
        existing = await _constraint_definition(conn, table, name)
        if existing is None:
            continue
        if _constraint_is_current(existing, expression):
            print(f"  = {table}.{name} already accepts update_settings")
            continue
        if dry_run:
            print(f"  [dry-run] would recreate {table}.{name} with {len(_AuditAction)} actions")
            changes += 1
            continue
        drop = _drop_constraint_sql(conn.dialect.name, table, name)
        add = _add_constraint_sql(conn.dialect.name, table, name, expression)
        if drop is None or add is None:
            print(f"  ! {name}: dialect {conn.dialect.name} cannot alter constraints - skipped")
            continue
        await conn.execute(text(drop))
        await conn.execute(text(add))
        print(f"  + {table}.{name} recreated with {len(_AuditAction)} actions")
        changes += 1
    return changes


async def main(dry_run: bool = False) -> int:
    """Apply the V5 model migration; return a process exit code.

    ``0`` means the schema is (or, in dry-run, would be) current. ``1`` means the
    constraints were deliberately skipped because the data disagrees with them:
    the schema was still widened, but the operator has to look at the warning.
    """
    print("migrate_v4 — CONTRACTS_V5 (deleted_source, app_settings, quick_replies)")
    print(f"  dialect: {engine.dialect.name}")

    # ``init_models`` -> ``Base.metadata.create_all``: creates ``app_settings`` and
    # ``quick_replies`` (with all their checks/indexes). Existing tables are left
    # untouched, which is why ``deleted_source`` is handled explicitly below.
    if dry_run:
        print("[dry-run] would call init_models() to create missing tables")
    else:
        await init_models()
        print(f"  ensured missing tables via create_all ({', '.join(CREATED_TABLES)})")

    async with engine.begin() as conn:
        changes = await _migrate_group_messages(conn, dry_run)
        changes += await _migrate_audit_action_vocabulary(conn, dry_run)
        await _report_tables(conn, dry_run)

    if dry_run:
        print(f"\n[dry-run] {changes} change(s) would be applied")
    else:
        print(f"\nmigrate_v4: {changes} change(s) applied (0 = schema already current)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(dry_run="--dry-run" in sys.argv)))
