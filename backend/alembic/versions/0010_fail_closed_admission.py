"""Fail-closed entry admission: geometry on intents, FKs, external incidents."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_fail_closed_admission"
down_revision: str | None = "0009_durable_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("order_intents") as batch:
        batch.add_column(sa.Column("geometry_hash", sa.String(64), nullable=True))

    op.create_table(
        "external_position_incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False, index=True),
        sa.Column("broker", sa.String(32), nullable=False),
        sa.Column("qty", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False, server_default="open"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_external_position_incidents_resolution",
        "external_position_incidents",
        ["resolution"],
    )

    # Entry purpose requires ApprovalAdmission FK for newly created intents.
    # Application layer enforces this for status=CREATED; broker gate re-checks.
    # Full DB CHECK deferred until production rows are backfilled.


def downgrade() -> None:
    op.drop_index("ix_external_position_incidents_resolution", "external_position_incidents")
    op.drop_table("external_position_incidents")

    with op.batch_alter_table("order_intents") as batch:
        batch.drop_column("geometry_hash")
