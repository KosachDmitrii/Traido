"""Alembic: one open position per symbol, enforced by the database.

The rule already existed in Python — the ledger refuses a second row under a
`threading.Lock`, and the execution service refuses the entry before that. Both
hold within one process; neither holds across two. A book with two open rows for
one symbol can never agree with a broker reporting a single net position, and
each row would carry its own protective stop for shares the other also claims.

**This migration fails on a database that already violates the rule, and that is
the intended behaviour.** A pre-flight query names the offending symbols and
tells the operator what to do, so the failure arrives as an instruction rather
than as an integrity error at deploy time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# Must fit alembic_version.version_num VARCHAR(32).
revision: str = "0006_one_open_pos_per_sym"
down_revision: str | None = "0005_stage71_exit_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ux_open_positions_one_open_per_symbol"


def _duplicates(bind: sa.engine.Connection) -> list[tuple[str, int]]:
    rows = bind.execute(
        sa.text(
            "SELECT symbol, COUNT(*) AS n FROM open_positions "
            "WHERE status = 'open' GROUP BY symbol HAVING COUNT(*) > 1"
        )
    ).all()
    return [(r[0], int(r[1])) for r in rows]


def upgrade() -> None:
    bind = op.get_bind()

    existing = _duplicates(bind)
    if existing:
        listed = ", ".join(f"{symbol} ({count} open rows)" for symbol, count in existing)
        raise RuntimeError(
            "Cannot enforce one open position per symbol: the book already holds "
            f"more than one for {listed}.\n\n"
            "This is a book that cannot be reconciled against a broker, so it has "
            "to be resolved by hand rather than by the migration guessing which "
            "row is real:\n"
            "  1. Read the broker's actual position for each symbol listed.\n"
            "  2. Keep the row whose quantity and entry match it; close the others "
            "with status='closed' and a note recording why.\n"
            "  3. Check that the surviving row's stop_order_id is a live order, and "
            "cancel any protective order belonging to the rows you closed.\n"
            "  4. Re-run the migration."
        )

    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        # Refused rather than silently created as a plain unique index, which
        # would reject every historical closed row for a symbol ever re-entered.
        raise RuntimeError(
            f"Partial unique indexes are not supported on {dialect!r}; "
            "the one-open-position-per-symbol invariant cannot be enforced here."
        )

    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON open_positions (symbol) WHERE status = 'open'"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
