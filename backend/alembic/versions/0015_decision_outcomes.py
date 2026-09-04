"""Durable decision-outcome ledger for funnel RCA.

Revision ID: 0015_decision_outcomes
Revises: 0014_strategy_promotion
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_decision_outcomes"
down_revision: str | None = "0014_strategy_promotion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("primary_reason", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("watch_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_outcomes_symbol", "decision_outcomes", ["symbol"])
    op.create_index("ix_decision_outcomes_outcome", "decision_outcomes", ["outcome"])
    op.create_index("ix_decision_outcomes_recorded_at", "decision_outcomes", ["recorded_at"])
    op.create_index(
        "ix_decision_outcomes_symbol_recorded",
        "decision_outcomes",
        ["symbol", "recorded_at"],
    )
    op.create_index(
        "ix_decision_outcomes_pipeline",
        "decision_outcomes",
        ["pipeline_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_decision_outcomes_pipeline", table_name="decision_outcomes")
    op.drop_index("ix_decision_outcomes_symbol_recorded", table_name="decision_outcomes")
    op.drop_index("ix_decision_outcomes_recorded_at", table_name="decision_outcomes")
    op.drop_index("ix_decision_outcomes_outcome", table_name="decision_outcomes")
    op.drop_index("ix_decision_outcomes_symbol", table_name="decision_outcomes")
    op.drop_table("decision_outcomes")
