"""Alembic: open_positions ledger for Stage 5."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_stage5_positions"
down_revision: str | None = "0002_stage4_desk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "open_positions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("qty", sa.Numeric(18, 8), nullable=False),
        sa.Column("avg_entry", sa.Numeric(18, 8), nullable=False),
        sa.Column("stop_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("target_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("strategy_version", sa.String(128), nullable=False),
        sa.Column("trading_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("entry_reasons", sa.JSON(), nullable=False),
        sa.Column("broker_entry_order_id", sa.String(128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_open_positions_opportunity_id", "open_positions", ["opportunity_id"])
    op.create_index("ix_open_positions_symbol", "open_positions", ["symbol"])
    op.create_index("ix_open_positions_status", "open_positions", ["status"])


def downgrade() -> None:
    op.drop_table("open_positions")
