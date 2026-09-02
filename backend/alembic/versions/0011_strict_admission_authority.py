"""Strict ApprovalAdmission authority: FKs, CHECKs, fingerprint column."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0011_strict_admission_authority"
down_revision: str | None = "0010_fail_closed_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # Leftover from a failed SQLite batch recreate.
    for tmp in ("_alembic_tmp_opportunities", "_alembic_tmp_order_intents"):
        if tmp in tables:
            op.drop_table(tmp)

    # Orphan table from an old path — not in models; drop so alembic check is clean.
    if "activity_events" in tables:
        for ix in inspector.get_indexes("activity_events"):
            op.drop_index(ix["name"], table_name="activity_events")
        op.drop_table("activity_events")

    cols = {c["name"] for c in inspector.get_columns("admission_records")}
    if "request_fingerprint" not in cols:
        with op.batch_alter_table("admission_records") as batch:
            batch.add_column(sa.Column("request_fingerprint", sa.String(64), nullable=True))

    existing_ix = {ix["name"] for ix in inspector.get_indexes("admission_records")}
    if "ix_admission_records_request_fingerprint" not in existing_ix:
        op.create_index(
            "ix_admission_records_request_fingerprint",
            "admission_records",
            ["request_fingerprint"],
        )

    # Legacy entry intents without ApprovalAdmission cannot satisfy the new
    # CHECK. They are not capital-path authority; drop them before the constraint.
    op.execute(
        sa.text(
            """
            DELETE FROM order_intents
            WHERE purpose = 'entry'
              AND (approval_admission_record_id IS NULL OR geometry_hash IS NULL)
            """
        )
    )

    # Refresh inspector after prior DDL.
    inspector = inspect(bind)
    opp_fks = {fk["name"] for fk in inspector.get_foreign_keys("opportunities")}
    if "fk_opportunities_approval_admission" not in opp_fks:
        with op.batch_alter_table(
            "opportunities",
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            batch.create_foreign_key(
                "fk_opportunities_approval_admission",
                "admission_records",
                ["approval_admission_record_id"],
                ["id"],
            )

    inspector = inspect(bind)
    intent_fks = {fk["name"] for fk in inspector.get_foreign_keys("order_intents")}
    intent_cks = {c["name"] for c in inspector.get_check_constraints("order_intents")}
    need_intent = (
        "fk_order_intents_approval_admission" not in intent_fks
        or "ck_entry_intent_has_approval_admission" not in intent_cks
        or "ck_entry_intent_has_geometry_hash" not in intent_cks
    )
    if need_intent:
        with op.batch_alter_table(
            "order_intents",
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            if "fk_order_intents_approval_admission" not in intent_fks:
                batch.create_foreign_key(
                    "fk_order_intents_approval_admission",
                    "admission_records",
                    ["approval_admission_record_id"],
                    ["id"],
                )
            if "ck_entry_intent_has_approval_admission" not in intent_cks:
                batch.create_check_constraint(
                    "ck_entry_intent_has_approval_admission",
                    "(purpose != 'entry') OR (approval_admission_record_id IS NOT NULL)",
                )
            if "ck_entry_intent_has_geometry_hash" not in intent_cks:
                batch.create_check_constraint(
                    "ck_entry_intent_has_geometry_hash",
                    "(purpose != 'entry') OR (geometry_hash IS NOT NULL)",
                )

    # Drop auto-named indexes left from earlier `index=True` columns so models
    # and the database agree on the migration-era index names.
    inspector = inspect(bind)
    obsolete = (
        ("entry_watches", "ix_entry_watches_converted_opportunity_id"),
        ("entry_watches", "ix_entry_watches_last_admission_record_id"),
        ("opportunities", "ix_opportunities_approval_admission_record_id"),
        ("opportunities", "ix_opportunities_creation_admission_record_id"),
        ("order_intents", "ix_order_intents_approval_admission_record_id"),
    )
    for table, ix_name in obsolete:
        present = {ix["name"] for ix in inspector.get_indexes(table)}
        if ix_name in present:
            op.drop_index(ix_name, table_name=table)


def downgrade() -> None:
    with op.batch_alter_table("order_intents") as batch:
        batch.drop_constraint("ck_entry_intent_has_geometry_hash", type_="check")
        batch.drop_constraint("ck_entry_intent_has_approval_admission", type_="check")
        batch.drop_constraint("fk_order_intents_approval_admission", type_="foreignkey")

    with op.batch_alter_table("opportunities") as batch:
        batch.drop_constraint("fk_opportunities_approval_admission", type_="foreignkey")

    op.drop_index("ix_admission_records_request_fingerprint", "admission_records")
    with op.batch_alter_table("admission_records") as batch:
        batch.drop_column("request_fingerprint")
