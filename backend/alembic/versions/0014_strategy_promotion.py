"""Strategy version registry and promotion gate (Stage 8).

Revision ID: 0014_strategy_promotion
Revises: 0013_final_admission_authority
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_strategy_promotion"
down_revision: str | None = "0013_final_admission_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version_tag", sa.String(length=64), nullable=False),
        sa.Column("parameter_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_strategy_versions_key"),
    )
    op.create_table(
        "strategy_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version_key", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verdict", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_strategy_evaluation_runs_strategy_version_key",
        "strategy_evaluation_runs",
        ["strategy_version_key"],
    )
    op.create_index(
        "ix_strategy_evaluation_runs_symbol",
        "strategy_evaluation_runs",
        ["symbol"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_evaluation_runs_symbol", table_name="strategy_evaluation_runs")
    op.drop_index(
        "ix_strategy_evaluation_runs_strategy_version_key",
        table_name="strategy_evaluation_runs",
    )
    op.drop_table("strategy_evaluation_runs")
    op.drop_table("strategy_versions")
