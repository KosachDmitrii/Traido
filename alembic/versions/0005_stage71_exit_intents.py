"""Alembic: generalize order intents to cover exits (Stage 7.1).

Additive only. Existing rows are entries by construction — Stage 7 had no other
kind — so the backfill is a constant and the migration is safe to run against a
live book.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_stage71_exit_intents"
down_revision: str | None = "0004_stage7_order_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "order_intents",
        sa.Column("purpose", sa.String(32), nullable=False, server_default="entry"),
    )
    op.add_column("order_intents", sa.Column("position_id", sa.Uuid(), nullable=True))
    op.create_index("ix_order_intents_purpose", "order_intents", ["purpose"])
    op.create_index("ix_order_intents_position_id", "order_intents", ["position_id"])


def downgrade() -> None:
    op.drop_index("ix_order_intents_position_id", table_name="order_intents")
    op.drop_index("ix_order_intents_purpose", table_name="order_intents")
    op.drop_column("order_intents", "position_id")
    op.drop_column("order_intents", "purpose")
