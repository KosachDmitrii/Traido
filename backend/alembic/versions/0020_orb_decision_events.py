"""Append-only evidence for every persisted ORB observation."""

import sqlalchemy as sa

from alembic import op

revision = "0020_orb_decision_events"
down_revision = "0019_market_bars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orb_decision_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_orb_decision_events_session", "orb_decision_events", ["session"])
    op.create_index("ix_orb_decision_events_symbol", "orb_decision_events", ["symbol"])
    op.create_index(
        "ix_orb_decision_events_session_symbol_time",
        "orb_decision_events",
        ["session", "symbol", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_orb_decision_events_session_symbol_time", table_name="orb_decision_events")
    op.drop_index("ix_orb_decision_events_symbol", table_name="orb_decision_events")
    op.drop_index("ix_orb_decision_events_session", table_name="orb_decision_events")
    op.drop_table("orb_decision_events")
