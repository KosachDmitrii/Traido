"""Alembic: durable order intents for Stage 7 execution hardening."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_stage7_order_intents"
down_revision: str | None = "0003_stage5_positions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("broker", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The uniqueness of the key is the real duplicate-order guard.
    op.create_index(
        "ix_order_intents_idempotency_key",
        "order_intents",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_order_intents_symbol", "order_intents", ["symbol"])
    op.create_index("ix_order_intents_status", "order_intents", ["status"])
    op.create_index("ix_order_intents_broker_order_id", "order_intents", ["broker_order_id"])
    op.create_index("ix_order_intents_opportunity_id", "order_intents", ["opportunity_id"])


def downgrade() -> None:
    op.drop_table("order_intents")
