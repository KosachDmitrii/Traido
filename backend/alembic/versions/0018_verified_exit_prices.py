"""Unknown exit prices are unknown results, never estimated break-even trades."""

import sqlalchemy as sa

from alembic import op

revision = "0018_verified_exit_prices"
down_revision = "0017_orb_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_journal") as batch:
        batch.alter_column("exit", existing_type=sa.Numeric(18, 8), nullable=True)
        batch.alter_column("pnl", existing_type=sa.Numeric(18, 4), nullable=True)
        batch.alter_column("pnl_pct", existing_type=sa.Float(), nullable=True)
    table = sa.table(
        "trade_journal",
        sa.column("id"),
        sa.column("exit"),
        sa.column("pnl"),
        sa.column("pnl_pct"),
        sa.column("exit_reasons", sa.JSON()),
        sa.column("assessments_at_entry", sa.JSON()),
    )
    db = op.get_bind()
    for row in db.execute(sa.select(table)).mappings():
        if "Reconcile: broker flat (stop or external close)" not in (row["exit_reasons"] or []):
            continue
        evidence = dict(row["assessments_at_entry"] or {})
        evidence["legacy_estimated_exit"] = {"exit": str(row["exit"]), "pnl": str(row["pnl"])}
        db.execute(
            table.update()
            .where(table.c.id == row["id"])
            .values(exit=None, pnl=None, pnl_pct=None, assessments_at_entry=evidence)
        )


def downgrade() -> None:
    # Replacing unknown results with numbers on rollback would fabricate history.
    raise RuntimeError("Restore verified exit prices before reverting this migration")
