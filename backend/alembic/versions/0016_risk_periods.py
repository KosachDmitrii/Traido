"""Account-scoped paper risk periods; no backfill or automatic baseline."""

import sqlalchemy as sa

from alembic import op

revision = "0016_risk_periods"
down_revision = "0015_decision_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_periods",
        sa.Column("account_key", sa.String(160), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("risk_periods")
