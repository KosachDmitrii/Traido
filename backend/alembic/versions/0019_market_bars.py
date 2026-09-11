"""Persist market source bars across restarts, separated by feed and timeframe."""

import sqlalchemy as sa

from alembic import op

revision = "0019_market_bars"
down_revision = "0018_verified_exit_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_bars",
        sa.Column("feed", sa.String(32), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("timeframe", sa.String(8), primary_key=True),
        sa.Column("timestamp", sa.String(40), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("market_bars")
