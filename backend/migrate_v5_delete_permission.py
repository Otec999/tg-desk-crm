"""Grant ``can_delete_messages`` to the seeded privileged accounts (idempotent).

Why this exists
---------------
CONTRACTS_V5 §1 lets an operator delete their own messages in Telegram from the CRM.
The UI gates the button on ``operator.can_delete_messages``, so the flag has to be
``true`` for the accounts the seed treats as privileged.

``seed.py`` already sets the flag when it *creates* an operator, but
:func:`seed._upsert_operators` deliberately leaves existing rows untouched (it must never
clobber a live account's password or role). On a database seeded before V5 the flag is
therefore still ``false`` everywhere, deletion is unreachable from the UI, and every
``DELETE /api/messages/{id}`` answers ``403``.

This script rewrites just that one boolean for the privileged usernames, and for the
administrator, so a pre-V5 installation behaves like a fresh one. Every other column is
left alone. Running it twice changes nothing the second time.

Usage
-----
    python migrate_v5_delete_permission.py            # apply
    python migrate_v5_delete_permission.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

sys.path.insert(0, __file__.rsplit("\\", 1)[0])

from sqlalchemy import select  # noqa: E402

from app.database import get_session_factory  # noqa: E402
from app.models import Operator  # noqa: E402

#: Accounts the seed considers privileged (see ``seed.PRIVILEGED_OPERATOR_USERNAMES``).
PRIVILEGED_USERNAMES: tuple[str, ...] = ("admin", "supervisor", "op1", "op3")
#: Roles that the delete service already treats as able to delete (``_operator_outranks``).
PRIVILEGED_ROLES: tuple[str, ...] = ("ADMIN", "SUPERVISOR")


@dataclass
class Report:
    """What the migration changed, for the operator to read."""

    updated: list[str] = field(default_factory=list)
    already_set: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return len(self.updated)


def _role_of(operator: Operator) -> str:
    """Normalise ``operator.role`` (an enum or a plain string) to a comparison key."""
    role = getattr(operator, "role", None)
    return str(getattr(role, "value", role) or "").upper()


async def run(dry_run: bool) -> Report:
    """Apply (or preview) the permission fix and return what happened."""
    report = Report()
    session_factory = get_session_factory()
    async with session_factory() as session:
        operators = (await session.execute(select(Operator).order_by(Operator.id))).scalars().all()

        # Read every column we need *before* the commit: ``commit()`` expires the
        # instances, and touching an expired attribute afterwards would trigger a lazy
        # refresh outside SQLAlchemy's greenlet context (``MissingGreenlet``).
        snapshot: list[tuple[Operator, str, str, bool]] = [
            (op, str(op.username), _role_of(op), bool(op.can_delete_messages)) for op in operators
        ]

        for operator, username, role, can_delete in snapshot:
            privileged = username in PRIVILEGED_USERNAMES or role in PRIVILEGED_ROLES
            if not privileged:
                continue
            if can_delete:
                report.already_set.append(username)
                continue
            operator.can_delete_messages = True
            report.updated.append(username)

        if not dry_run and report.updated:
            await session.commit()
        elif not dry_run:
            await session.rollback()

        # Report usernames the seed expects but that are absent, so a typo does not hide.
        present = {username for _, username, _, _ in snapshot}
        report.missing = [u for u in PRIVILEGED_USERNAMES if u not in present]

    return report


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grant can_delete_messages to seeded accounts.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the changes without writing them",
    )
    args = parser.parse_args(argv)

    print("=" * 66)
    print("  V5 delete permission migration" + ("  [DRY RUN]" if args.dry_run else ""))
    print("=" * 66)

    report = await run(dry_run=args.dry_run)

    for name in report.updated:
        print(f"  {'would grant' if args.dry_run else 'granted'}  can_delete_messages -> {name}")
    for name in report.already_set:
        print(f"  already set     {name}")
    for name in report.missing:
        print(f"  not in database {name} (skipped)")

    verb = "would change" if args.dry_run else "change(s) applied"
    print("-" * 66)
    print(f"  {report.change_count} {verb}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
