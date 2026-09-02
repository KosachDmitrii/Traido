"""Production-ready ApprovalCommand columns + archive (no silent DELETE).

Revision ID: 0012_approval_command_evidence
Revises: 0011_strict_admission_authority
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_approval_command_evidence"
down_revision: Union[str, None] = "0011_strict_admission_authority"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- Archive tables (preserve legacy rows instead of DROP/DELETE) ---
    if "archived_entry_intents" not in inspector.get_table_names():
        op.create_table(
            "archived_entry_intents",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("archive_reason", sa.String(128), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )

    if "archived_activity_events" not in inspector.get_table_names():
        op.create_table(
            "archived_activity_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("archive_reason", sa.String(128), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )

    # If activity_events still exists (pre-0011 DBs), archive every row then drop.
    tables = set(inspector.get_table_names())
    if "activity_events" in tables:
        conn = op.get_bind()
        src_count = int(conn.execute(sa.text("SELECT COUNT(*) FROM activity_events")).scalar() or 0)
        if src_count:
            rows = conn.execute(sa.text("SELECT * FROM activity_events")).mappings().all()
            for row in rows:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO archived_activity_events (archived_at, archive_reason, payload)
                        VALUES (CURRENT_TIMESTAMP, :reason, :payload)
                        """
                    ),
                    {
                        "reason": "pre_0012_activity_events_row",
                        "payload": dict(row),
                    },
                )
            archived = int(
                conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM archived_activity_events "
                        "WHERE archive_reason = 'pre_0012_activity_events_row'"
                    )
                ).scalar()
                or 0
            )
            if archived != src_count:
                raise RuntimeError(
                    f"activity_events archive mismatch: source={src_count} archived={archived}"
                )
        op.drop_table("activity_events")

    # --- admission_records: request_id + pipeline index ---
    adm_cols = {c["name"] for c in inspector.get_columns("admission_records")}
    with op.batch_alter_table("admission_records") as batch:
        if "request_id" not in adm_cols:
            batch.add_column(sa.Column("request_id", sa.String(32), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_admission_records_pipeline_run_id "
        "ON admission_records (pipeline_run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_admission_records_request_id "
        "ON admission_records (request_id)"
    )

    # --- order_intents: request_id + fingerprint columns ---
    intent_cols = {c["name"] for c in inspector.get_columns("order_intents")}
    with op.batch_alter_table("order_intents") as batch:
        if "request_id" not in intent_cols:
            batch.add_column(sa.Column("request_id", sa.String(32), nullable=True))
        if "request_fingerprint" not in intent_cols:
            batch.add_column(sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_order_intents_request_id "
        "ON order_intents (request_id)"
    )
    # Archive legacy entry intents missing admission/hash instead of deleting.
    conn = op.get_bind()
    legacy = conn.execute(
        sa.text(
            """
            SELECT id, payload FROM order_intents
            WHERE purpose = 'entry'
              AND (approval_admission_record_id IS NULL OR geometry_hash IS NULL)
            """
        )
    ).fetchall()
    for row in legacy:
        conn.execute(
            sa.text(
                """
                INSERT INTO archived_entry_intents (id, archived_at, archive_reason, payload)
                VALUES (:id, CURRENT_TIMESTAMP, 'missing_admission_or_geometry', :payload)
                """
            ),
            {"id": str(row[0]).replace("-", ""), "payload": row[1]},
        )
    if legacy:
        archived_n = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM archived_entry_intents "
                "WHERE archive_reason = 'missing_admission_or_geometry'"
            )
        ).scalar()
        if archived_n != len(legacy):
            raise RuntimeError(
                f"legacy intent archive mismatch: source={len(legacy)} archived={archived_n}"
            )
        conn.execute(
            sa.text(
                """
                DELETE FROM order_intents
                WHERE purpose = 'entry'
                  AND (approval_admission_record_id IS NULL OR geometry_hash IS NULL)
                """
            )
        )

    # Unique: one entry attempt per opportunity + request_id (when request_id set).
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_entry_intent_opportunity_request
        ON order_intents (opportunity_id, request_id)
        WHERE purpose = 'entry' AND request_id IS NOT NULL
        """
    )

    # --- external_position_incidents: correlation columns + open unique ---
    epi_cols = {c["name"] for c in inspector.get_columns("external_position_incidents")}
    with op.batch_alter_table("external_position_incidents") as batch:
        for name, col in (
            ("account_id", sa.Column("account_id", sa.String(64), nullable=True)),
            ("broker_order_id", sa.Column("broker_order_id", sa.String(128), nullable=True)),
            ("broker_perm_id", sa.Column("broker_perm_id", sa.String(128), nullable=True)),
            ("client_order_id", sa.Column("client_order_id", sa.String(128), nullable=True)),
            (
                "correlation_status",
                sa.Column("correlation_status", sa.String(32), nullable=True),
            ),
        ):
            if name not in epi_cols:
                batch.add_column(col)

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_open_external_position_incident
        ON external_position_incidents (broker, account_id, symbol)
        WHERE resolution = 'open'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_open_external_position_incident")
    op.execute("DROP INDEX IF EXISTS uq_entry_intent_opportunity_request")
    op.execute("DROP INDEX IF EXISTS ix_admission_records_request_id")
    op.execute("DROP INDEX IF EXISTS ix_admission_records_pipeline_run_id")
    with op.batch_alter_table("order_intents") as batch:
        batch.drop_column("request_fingerprint")
        batch.drop_column("request_id")
    with op.batch_alter_table("admission_records") as batch:
        batch.drop_column("request_id")
    # Archive tables retained on downgrade (irreversible boundary for audit data).
