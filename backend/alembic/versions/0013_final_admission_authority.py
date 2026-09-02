"""Final admission authority: broker identity columns + entry intent CHECKs.

Revision ID: 0013_final_admission_authority
Revises: 0012_approval_command_evidence
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0013_final_admission_authority"
down_revision: str | None = "0012_approval_command_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _archive_legacy_entry_intents(conn: sa.Connection) -> None:
    legacy = conn.execute(
        sa.text(
            """
            SELECT id, payload FROM order_intents
            WHERE purpose = 'entry'
              AND (
                    request_id IS NULL
                 OR request_fingerprint IS NULL
                 OR broker IS NULL
                 OR broker_account_id IS NULL
                 OR broker_environment IS NULL
                 OR broker_environment != 'paper'
              )
            """
        )
    ).fetchall()
    for row in legacy:
        conn.execute(
            sa.text(
                """
                INSERT INTO archived_entry_intents (id, archived_at, archive_reason, payload)
                VALUES (:id, CURRENT_TIMESTAMP, 'missing_broker_authority_fields', :payload)
                """
            ),
            {"id": str(row[0]).replace("-", ""), "payload": row[1]},
        )
    if legacy:
        archived_n = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM archived_entry_intents "
                "WHERE archive_reason = 'missing_broker_authority_fields'"
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
                  AND (
                        request_id IS NULL
                     OR request_fingerprint IS NULL
                     OR broker IS NULL
                     OR broker_account_id IS NULL
                     OR broker_environment IS NULL
                     OR broker_environment != 'paper'
                  )
                """
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    conn = bind

    intent_cols = {c["name"] for c in inspector.get_columns("order_intents")}
    with op.batch_alter_table("order_intents") as batch:
        if "broker_account_id" not in intent_cols:
            batch.add_column(sa.Column("broker_account_id", sa.String(64), nullable=True))
        if "broker_environment" not in intent_cols:
            batch.add_column(
                sa.Column(
                    "broker_environment",
                    sa.String(16),
                    nullable=True,
                    server_default="paper",
                )
            )

    # Backfill dedicated columns from JSON payload when present.
    if bind.dialect.name == "sqlite":
        conn.execute(
            sa.text(
                """
                UPDATE order_intents
                SET broker_account_id = COALESCE(
                    broker_account_id,
                    json_extract(payload, '$.broker_account_id')
                )
                WHERE purpose = 'entry'
                """
            )
        )
        conn.execute(
            sa.text(
                """
                UPDATE order_intents
                SET broker_environment = COALESCE(
                    broker_environment,
                    json_extract(payload, '$.broker_environment'),
                    'paper'
                )
                WHERE purpose = 'entry'
                """
            )
        )
    else:
        conn.execute(
            sa.text(
                """
                UPDATE order_intents
                SET broker_account_id = COALESCE(
                    broker_account_id,
                    payload->>'broker_account_id'
                )
                WHERE purpose = 'entry'
                """
            )
        )
        conn.execute(
            sa.text(
                """
                UPDATE order_intents
                SET broker_environment = COALESCE(
                    broker_environment,
                    payload->>'broker_environment',
                    'paper'
                )
                WHERE purpose = 'entry'
                """
            )
        )

    conn.execute(
        sa.text(
            """
            UPDATE order_intents
            SET broker_environment = 'paper'
            WHERE purpose = 'entry' AND broker_environment IS NULL
            """
        )
    )

    _archive_legacy_entry_intents(conn)

    # Backfill approval-phase admission request_id from context for reads.
    if bind.dialect.name == "sqlite":
        conn.execute(
            sa.text(
                """
                UPDATE admission_records
                SET request_id = REPLACE(json_extract(payload, '$.context.request_id'), '-', '')
                WHERE phase = 'approval'
                  AND request_id IS NULL
                  AND json_extract(payload, '$.context.request_id') IS NOT NULL
                """
            )
        )
    else:
        conn.execute(
            sa.text(
                """
                UPDATE admission_records
                SET request_id = REPLACE(payload->'context'->>'request_id', '-', '')
                WHERE phase = 'approval'
                  AND request_id IS NULL
                  AND payload->'context'->>'request_id' IS NOT NULL
                """
            )
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_admission_records_request_id "
        "ON admission_records (request_id)"
    )

    inspector = inspect(bind)
    intent_cks = {c["name"] for c in inspector.get_check_constraints("order_intents")}
    new_checks = (
        ("ck_entry_intent_has_request_id", "(purpose != 'entry') OR (request_id IS NOT NULL)"),
        (
            "ck_entry_intent_has_request_fingerprint",
            "(purpose != 'entry') OR (request_fingerprint IS NOT NULL)",
        ),
        ("ck_entry_intent_has_broker", "(purpose != 'entry') OR (broker IS NOT NULL)"),
        (
            "ck_entry_intent_has_broker_account_id",
            "(purpose != 'entry') OR (broker_account_id IS NOT NULL)",
        ),
        (
            "ck_entry_intent_paper_environment",
            "(purpose != 'entry') OR (broker_environment = 'paper')",
        ),
    )
    need_intent = any(name not in intent_cks for name, _ in new_checks)
    if need_intent:
        with op.batch_alter_table(
            "order_intents",
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            for name, expr in new_checks:
                if name not in intent_cks:
                    batch.create_check_constraint(name, expr)

    violating_adm = int(
        conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM admission_records "
                "WHERE phase = 'approval' AND request_id IS NULL"
            )
        ).scalar()
        or 0
    )
    adm_cks = {c["name"] for c in inspector.get_check_constraints("admission_records")}
    if violating_adm == 0 and "ck_approval_admission_has_request_id" not in adm_cks:
        with op.batch_alter_table(
            "admission_records",
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            batch.create_check_constraint(
                "ck_approval_admission_has_request_id",
                "(phase != 'approval') OR (request_id IS NOT NULL)",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    adm_cks = {c["name"] for c in inspector.get_check_constraints("admission_records")}
    if "ck_approval_admission_has_request_id" in adm_cks:
        with op.batch_alter_table("admission_records") as batch:
            batch.drop_constraint("ck_approval_admission_has_request_id", type_="check")

    intent_cks = {c["name"] for c in inspector.get_check_constraints("order_intents")}
    drop_checks = (
        "ck_entry_intent_paper_environment",
        "ck_entry_intent_has_broker_account_id",
        "ck_entry_intent_has_broker",
        "ck_entry_intent_has_request_fingerprint",
        "ck_entry_intent_has_request_id",
    )
    if any(name in intent_cks for name in drop_checks):
        with op.batch_alter_table(
            "order_intents",
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            for name in drop_checks:
                if name in intent_cks:
                    batch.drop_constraint(name, type_="check")

    intent_cols = {c["name"] for c in inspector.get_columns("order_intents")}
    with op.batch_alter_table("order_intents") as batch:
        if "broker_environment" in intent_cols:
            batch.drop_column("broker_environment")
        if "broker_account_id" in intent_cols:
            batch.drop_column("broker_account_id")
