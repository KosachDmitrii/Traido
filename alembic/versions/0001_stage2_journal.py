"""Stage 2 — backtest_runs + trade_journal.

Revision ID: 0001_stage2_journal
Revises:
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_stage2_journal"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("strategy_version", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("starting_equity", sa.Numeric(18, 4), nullable=False),
        sa.Column("ending_equity", sa.Numeric(18, 4), nullable=False),
        sa.Column("net_pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("win_count", sa.Integer(), nullable=False),
        sa.Column("loss_count", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Float(), nullable=False),
        sa.Column("profit_factor", sa.Float(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=False),
        sa.Column("avg_r", sa.Float(), nullable=True),
        sa.Column("avg_bars_held", sa.Float(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_table(
        "trade_journal",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("backtest_run_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("entry", sa.Numeric(18, 8), nullable=False),
        sa.Column("exit", sa.Numeric(18, 8), nullable=False),
        sa.Column("stop", sa.Numeric(18, 8), nullable=True),
        sa.Column("target", sa.Numeric(18, 8), nullable=True),
        sa.Column("qty", sa.Numeric(18, 8), nullable=False),
        sa.Column("pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("pnl_pct", sa.Float(), nullable=False),
        sa.Column("mfe", sa.Numeric(18, 8), nullable=True),
        sa.Column("mae", sa.Numeric(18, 8), nullable=True),
        sa.Column("mfe_pct", sa.Float(), nullable=True),
        sa.Column("mae_pct", sa.Float(), nullable=True),
        sa.Column("max_drawdown_during", sa.Float(), nullable=True),
        sa.Column("entry_reasons", sa.JSON(), nullable=False),
        sa.Column("exit_reasons", sa.JSON(), nullable=False),
        sa.Column("strategy_version", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=True),
        sa.Column("trading_mode", sa.String(32), nullable=False),
        sa.Column("indicators_at_entry", sa.JSON(), nullable=False),
        sa.Column("assessments_at_entry", sa.JSON(), nullable=False),
        sa.Column("market_regime", sa.String(64), nullable=True),
        sa.Column("risk_reward_planned", sa.Float(), nullable=True),
        sa.Column("bars_held", sa.Integer(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_trade_journal_backtest_run_id", "trade_journal", ["backtest_run_id"])
    op.create_index("ix_trade_journal_symbol", "trade_journal", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_trade_journal_symbol", table_name="trade_journal")
    op.drop_index("ix_trade_journal_backtest_run_id", table_name="trade_journal")
    op.drop_table("trade_journal")
    op.drop_table("backtest_runs")
