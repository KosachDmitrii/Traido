"""ORB reporting excludes other strategy versions and simulated backtests."""

from uuid import uuid4

from sqlalchemy import create_engine

from database.models.journal import TradeJournalRow
from database.session import init_db, session_factory
from strategy.orb import VERSION
from strategy.orb.evaluation import paper_evaluation


def test_orb_results_are_isolated(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'eval.db'}")
    init_db(engine)
    empty = paper_evaluation(engine=engine)
    assert empty["trade_count"] == 0
    assert empty["pnl"] is None and empty["win_rate"] is None
    with session_factory(engine)() as db:
        for pnl, version, backtest in [
            (10, VERSION, None),
            (-5, VERSION, None),
            (0, VERSION, None),
            (999, "old@1", None),
            (999, VERSION, uuid4()),
        ]:
            db.add(
                TradeJournalRow(
                    symbol="AAPL",
                    entry=100,
                    exit=100 + pnl,
                    qty=1,
                    pnl=pnl,
                    pnl_pct=pnl,
                    strategy_version=version,
                    backtest_run_id=backtest,
                )
            )
        db.commit()
    report = paper_evaluation(engine=engine)
    assert report["trade_count"] == 3
    assert (report["wins"], report["losses"], report["breakeven"]) == (1, 1, 1)
    assert float(report["pnl"]) == 5
    assert report["win_rate"] == 1 / 3
    assert report["profit_factor"] == 2
    assert abs(float(report["expectancy"]) - 5 / 3) < 0.001
