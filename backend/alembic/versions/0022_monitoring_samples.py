"""Add independent forward observation evidence; trading tables untouched."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0022_monitoring_samples"
down_revision = "0021_sector_classifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitoring_samples",
        sa.Column("minute", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("session", sa.String(10), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )
    op.create_index("ix_monitoring_samples_session", "monitoring_samples", ["session"])
    op.create_index("ix_monitoring_samples_policy_hash", "monitoring_samples", ["policy_hash"])
    op.create_table(
        "monitoring_reports",
        sa.Column("session", sa.String(10), primary_key=True),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("monitoring_reports")
    op.drop_table("monitoring_samples")
