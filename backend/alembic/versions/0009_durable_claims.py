"""Durable claims, lease expiry, and approval admission linkage."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_durable_claims"
down_revision: str | None = "0008_production_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("entry_watches") as batch:
        batch.add_column(sa.Column("claim_owner_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("opportunities") as batch:
        batch.add_column(sa.Column("creation_admission_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("approval_admission_record_id", sa.Uuid(), nullable=True))

    op.create_index(
        "ix_opportunities_approval_admission",
        "opportunities",
        ["approval_admission_record_id"],
    )

    with op.batch_alter_table("order_intents") as batch:
        batch.add_column(sa.Column("approval_admission_record_id", sa.Uuid(), nullable=True))

    op.create_index(
        "ix_order_intents_approval_admission",
        "order_intents",
        ["approval_admission_record_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_intents_approval_admission", "order_intents")
    with op.batch_alter_table("order_intents") as batch:
        batch.drop_column("approval_admission_record_id")

    op.drop_index("ix_opportunities_approval_admission", "opportunities")
    with op.batch_alter_table("opportunities") as batch:
        batch.drop_column("approval_admission_record_id")
        batch.drop_column("creation_admission_version")

    with op.batch_alter_table("entry_watches") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("claim_owner_id")
