"""Persist timestamped and versioned Finnhub classification evidence."""

import sqlalchemy as sa

from alembic import op

revision = "0021_sector_classifications"
down_revision = "0020_orb_decision_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sector_classifications",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sector_classifications")
