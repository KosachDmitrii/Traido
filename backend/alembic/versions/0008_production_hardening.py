"""Production hardening — watch CAS columns, admission keys, opportunity legacy."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_production_hardening"
down_revision: str | None = "0007_phase3_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATUSES = ("waiting", "triggered", "revalidating", "admitted", "converting")


def upgrade() -> None:
    # ── entry_watches ────────────────────────────────────────────────────────
    with op.batch_alter_table("entry_watches") as batch:
        batch.add_column(sa.Column("strategy_version", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("state_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("trigger_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("claim_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_admission_record_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("converted_opportunity_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("exec_timeframe", sa.String(16), nullable=True))
        batch.add_column(sa.Column("geometry_hash", sa.String(64), nullable=True))

    op.create_index(
        "ix_entry_watches_last_admission",
        "entry_watches",
        ["last_admission_record_id"],
    )
    op.create_index(
        "ix_entry_watches_converted_opp",
        "entry_watches",
        ["converted_opportunity_id"],
        unique=True,
        sqlite_where=sa.text("converted_opportunity_id IS NOT NULL"),
        postgresql_where=sa.text("converted_opportunity_id IS NOT NULL"),
    )
    op.create_index(
        "ix_entry_watches_active_symbol_strategy",
        "entry_watches",
        ["symbol", "strategy_version"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('waiting','triggered','revalidating','admitted','converting')"
        ),
        postgresql_where=sa.text(
            "status IN ('waiting','triggered','revalidating','admitted','converting')"
        ),
    )

    # Legacy rows without geometry — invalidate rather than trade.
    op.execute(
        sa.text(
            """
            UPDATE entry_watches
            SET status = 'invalidated'
            WHERE geometry_hash IS NULL
              AND status IN ('waiting','triggered','revalidating','admitted','converting')
            """
        )
    )

    # ── admission_records ────────────────────────────────────────────────────
    with op.batch_alter_table("admission_records") as batch:
        batch.add_column(sa.Column("evaluation_key", sa.String(256), nullable=True))
        batch.add_column(sa.Column("phase", sa.String(32), nullable=True))
        batch.add_column(sa.Column("geometry_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("quote_ts", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("market_gate_ts", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("source_version", sa.String(64), nullable=True))

    op.create_index(
        "ix_admission_records_evaluation_key",
        "admission_records",
        ["evaluation_key"],
        unique=True,
        sqlite_where=sa.text("evaluation_key IS NOT NULL"),
        postgresql_where=sa.text("evaluation_key IS NOT NULL"),
    )

    # ── opportunities ──────────────────────────────────────────────────────
    with op.batch_alter_table("opportunities") as batch:
        batch.add_column(sa.Column("creation_admission_record_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("geometry_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("policy_version", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("legacy", sa.Boolean(), nullable=False, server_default=sa.text("true"))
        )
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("decision_version", sa.Integer(), nullable=False, server_default="0")
        )

    op.create_index(
        "ix_opportunities_creation_admission",
        "opportunities",
        ["creation_admission_record_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_opportunities_creation_admission", "opportunities")
    with op.batch_alter_table("opportunities") as batch:
        batch.drop_column("decision_version")
        batch.drop_column("approved_at")
        batch.drop_column("legacy")
        batch.drop_column("policy_version")
        batch.drop_column("geometry_hash")
        batch.drop_column("creation_admission_record_id")

    op.drop_index("ix_admission_records_evaluation_key", "admission_records")
    with op.batch_alter_table("admission_records") as batch:
        batch.drop_column("source_version")
        batch.drop_column("expires_at")
        batch.drop_column("market_gate_ts")
        batch.drop_column("quote_ts")
        batch.drop_column("geometry_hash")
        batch.drop_column("phase")
        batch.drop_column("evaluation_key")

    op.drop_index("ix_entry_watches_active_symbol_strategy", "entry_watches")
    op.drop_index("ix_entry_watches_converted_opp", "entry_watches")
    op.drop_index("ix_entry_watches_last_admission", "entry_watches")
    with op.batch_alter_table("entry_watches") as batch:
        batch.drop_column("geometry_hash")
        batch.drop_column("exec_timeframe")
        batch.drop_column("converted_opportunity_id")
        batch.drop_column("last_admission_record_id")
        batch.drop_column("triggered_at")
        batch.drop_column("claim_token")
        batch.drop_column("claimed_at")
        batch.drop_column("trigger_version")
        batch.drop_column("state_version")
        batch.drop_column("strategy_version")
