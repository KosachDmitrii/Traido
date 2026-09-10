"""Persist ORB selections without rewriting historical trades."""

import sqlalchemy as sa

from alembic import op

revision = "0017_orb_sessions"
down_revision = "0016_risk_periods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orb_sessions",
        sa.Column("session", sa.String(10), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("orb_sessions")
