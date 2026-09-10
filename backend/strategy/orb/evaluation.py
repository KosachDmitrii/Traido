"""Read-only results for the active ORB version; never mix legacy or backtest trades."""

from sqlalchemy import case, func

from database.models.journal import TradeJournalRow
from database.session import session_factory
from strategy.orb import VERSION


def paper_evaluation(*, engine=None) -> dict:
    with session_factory(engine)() as db:
        row = (
            db.query(
                func.count(TradeJournalRow.id),
                func.sum(case((TradeJournalRow.pnl > 0, 1), else_=0)),
                func.sum(case((TradeJournalRow.pnl < 0, 1), else_=0)),
                func.sum(TradeJournalRow.pnl),
                func.sum(case((TradeJournalRow.pnl > 0, TradeJournalRow.pnl), else_=0)),
                func.sum(case((TradeJournalRow.pnl < 0, -TradeJournalRow.pnl), else_=0)),
                func.min(TradeJournalRow.closed_at),
                func.max(TradeJournalRow.closed_at),
            )
            .filter(
                TradeJournalRow.strategy_version == VERSION,
                TradeJournalRow.backtest_run_id.is_(None),
            )
            .one()
        )
        count, wins, losses, pnl, gross_profit, gross_loss, first, last = row
        return {
            "strategy_version": VERSION,
            "trade_count": count,
            "wins": int(wins or 0),
            "losses": int(losses or 0),
            "breakeven": count - int(wins or 0) - int(losses or 0),
            "pnl": str(pnl) if count else None,
            "win_rate": float(wins) / count if count else None,
            "expectancy": str(pnl / count) if count else None,
            "profit_factor": float(gross_profit / gross_loss) if gross_loss else None,
            "first_closed_at": first.isoformat() if first else None,
            "last_closed_at": last.isoformat() if last else None,
        }
