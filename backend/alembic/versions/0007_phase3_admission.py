"""Phase 3 — durable entry watches, admission records, shadow outcomes."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_phase3_admission"
down_revision: str | None = "0006_one_open_pos_per_sym"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entry_watches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_entry_watches_symbol", "entry_watches", ["symbol"])
    op.create_index("ix_entry_watches_status", "entry_watches", ["status"])

    op.create_table(
        "admission_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("watch_id", sa.Uuid(), nullable=True),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_admission_records_symbol", "admission_records", ["symbol"])
    op.create_index("ix_admission_records_recorded_at", "admission_records", ["recorded_at"])
    op.create_index("ix_admission_records_decision", "admission_records", ["decision"])
    op.create_index("ix_admission_records_watch_id", "admission_records", ["watch_id"])
    op.create_index("ix_admission_records_opportunity_id", "admission_records", ["opportunity_id"])

    op.create_table(
        "shadow_outcomes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shadow_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("watch_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_shadow_outcomes_symbol", "shadow_outcomes", ["symbol"])
    op.create_index("ix_shadow_outcomes_status", "shadow_outcomes", ["status"])
    op.create_index("ix_shadow_outcomes_recorded_at", "shadow_outcomes", ["recorded_at"])
    op.create_index("ix_shadow_outcomes_watch_id", "shadow_outcomes", ["watch_id"])


def downgrade() -> None:
    op.drop_table("shadow_outcomes")
    op.drop_table("admission_records")
    op.drop_table("entry_watches")
