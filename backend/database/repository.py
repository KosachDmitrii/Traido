"""Persist BacktestSummary into journal tables."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.engine import Engine

from core.enums import TradingMode
from core.schemas import BacktestSummary
from database.models.journal import BacktestRunRow, TradeJournalRow
from database.session import session_factory


def persist_backtest_summary(
    summary: BacktestSummary,
    *,
    params: dict[str, Any] | None = None,
    notes: str | None = None,
    trading_mode: TradingMode = TradingMode.CONFIRMATION,
    engine: Engine | None = None,
) -> uuid.UUID:
    SessionLocal = session_factory(engine)
    run_id = uuid.uuid4()
    with SessionLocal() as session:
        session.add(
            BacktestRunRow(
                id=run_id,
                status="completed",
                strategy_version=summary.strategy_version,
                symbol=summary.symbol,
                timeframe=summary.timeframe.value,
                starting_equity=summary.starting_equity,
                ending_equity=summary.ending_equity,
                net_pnl=summary.net_pnl,
                return_pct=summary.return_pct,
                trade_count=summary.trade_count,
                win_count=summary.win_count,
                loss_count=summary.loss_count,
                win_rate=summary.win_rate,
                profit_factor=summary.profit_factor,
                max_drawdown_pct=summary.max_drawdown_pct,
                avg_r=summary.avg_r,
                avg_bars_held=summary.avg_bars_held,
                params=params or {},
                notes=notes,
            )
        )
        for trade in summary.trades:
            session.add(
                TradeJournalRow(
                    id=uuid.uuid4(),
                    backtest_run_id=run_id,
                    position_id=uuid.uuid4(),
                    symbol=trade.symbol,
                    entry=trade.entry,
                    exit=trade.exit,
                    stop=trade.stop,
                    target=trade.target,
                    qty=trade.qty,
                    pnl=trade.pnl,
                    pnl_pct=trade.pnl_pct,
                    mfe=trade.mfe,
                    mae=trade.mae,
                    mfe_pct=trade.mfe_pct,
                    mae_pct=trade.mae_pct,
                    entry_reasons=trade.entry_reasons,
                    exit_reasons=trade.exit_reasons,
                    strategy_version=trade.strategy_version,
                    trading_mode=trading_mode.value,
                    indicators_at_entry=trade.indicators_at_entry,
                    risk_reward_planned=trade.risk_reward_planned,
                    bars_held=trade.bars_held,
                    opened_at=trade.opened_at,
                    closed_at=trade.closed_at,
                )
            )
        session.commit()
    return run_id


def list_journal_for_run(
    run_id: uuid.UUID, *, engine: Engine | None = None
) -> list[TradeJournalRow]:
    SessionLocal = session_factory(engine)
    with SessionLocal() as session:
        return list(
            session.query(TradeJournalRow)
            .filter(TradeJournalRow.backtest_run_id == run_id)
            .order_by(TradeJournalRow.opened_at.asc())
            .all()
        )
