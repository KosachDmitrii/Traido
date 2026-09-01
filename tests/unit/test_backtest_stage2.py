"""Stage 2 — backtest engine, metrics, journal persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.enums import ExitReason, Timeframe
from core.schemas import Bar
from database.base import Base
from database.models.journal import BacktestRunRow, TradeJournalRow
from database.repository import persist_backtest_summary
from market_data.providers.fixture import FixtureMarketData
from quant.backtesting.engine import BacktestEngine
from quant.backtesting.metrics import max_drawdown_pct, profit_factor, win_rate
from quant.backtesting.strategy import EmaTrendStub
from quant.costs import CostModel


def _once_entry_strategy() -> EmaTrendStub:
    """Strategy that enters exactly once, on the 6th bar."""

    class OnceEntry(EmaTrendStub):
        version = "once@test"

        def warm_up(self) -> int:
            return 5

        def evaluate_entry(self, bars):  # type: ignore[no-untyped-def]
            from quant.backtesting.strategy import EntrySignal

            if len(bars) == 6:
                return EntrySignal(reasons=["test entry"], stop_distance_pct=0.02)
            return None

        def evaluate_exit(self, bars, entry_price):  # type: ignore[no-untyped-def]
            return None

    return OnceEntry()


def _stop_gap_bars() -> list[Bar]:
    """Flat series, entry on bar 6, then a bar spanning both stop and target."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    px = 100.0
    for i in range(8):
        if i == 6:
            o = c = 100.0
            h, l = 100.5, 99.5
        elif i == 7:
            o, h, l, c = 100.0, 110.0, 90.0, 105.0
        else:
            o = c = px
            h, l = px + 0.2, px - 0.2
        bars.append(
            Bar(
                symbol="X",
                timeframe=Timeframe.D1,
                ts=start + timedelta(days=i),
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(l)),
                close=Decimal(str(c)),
                volume=Decimal(1000),
                source="t",
            )
        )
    return bars


def _synthetic_uptrend(n: int = 260) -> list[Bar]:
    bars: list[Bar] = []
    price = 100.0
    start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    for i in range(n):
        o = price
        c = price + 0.25 + ((i % 9) - 4) * 0.05
        h = max(o, c) + 0.4
        l = min(o, c) - 0.35
        bars.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.D1,
                ts=start + timedelta(days=i),
                open=Decimal(str(round(o, 4))),
                high=Decimal(str(round(h, 4))),
                low=Decimal(str(round(l, 4))),
                close=Decimal(str(round(c, 4))),
                volume=Decimal(1000000),
                source="synthetic",
            )
        )
        price = c
    return bars


def test_max_drawdown_pct() -> None:
    assert max_drawdown_pct([100, 110, 99, 105]) == pytest.approx(10.0)


def test_win_rate_and_profit_factor_empty() -> None:
    assert win_rate([]) == 0.0
    assert profit_factor([]) is None


def test_backtest_runs_without_lookahead_crash() -> None:
    bars = _synthetic_uptrend(260)
    strategy = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=30, rsi_max=80)
    engine = BacktestEngine(strategy, starting_equity=Decimal(100000), risk_per_trade_pct=1.0)
    summary = engine.run("TEST", Timeframe.D1, bars)
    assert summary.trade_count >= 0
    assert summary.ending_equity > 0
    assert summary.max_drawdown_pct >= 0
    for t in summary.trades:
        assert t.mfe >= 0
        assert t.mae >= 0
        assert t.bars_held >= 1
        assert t.exit_reasons
        assert t.stop < t.entry < t.target or ExitReason.END_OF_DATA.value in t.exit_reasons


@pytest.mark.asyncio
async def test_backtest_on_fixture_aapl() -> None:
    md = FixtureMarketData()
    bars = await md.get_bars(
        "AAPL",
        Timeframe.D1,
        datetime(2023, 1, 1, tzinfo=UTC),
        datetime(2025, 12, 31, tzinfo=UTC),
    )
    assert len(bars) >= 220
    strategy = EmaTrendStub(ema_fast=20, ema_slow=50)
    summary = BacktestEngine(strategy).run("AAPL", Timeframe.D1, bars)
    assert summary.symbol == "AAPL"
    assert summary.strategy_version.startswith("ema_trend_stub")
    # Journal fields present even if zero trades on this path
    assert isinstance(summary.trades, list)


def test_persist_journal_sqlite(tmp_path) -> None:
    bars = _synthetic_uptrend(260)
    strategy = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=35, rsi_max=70)
    summary = BacktestEngine(strategy).run("TEST", Timeframe.D1, bars)

    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    Base.metadata.create_all(eng)
    run_id = persist_backtest_summary(summary, params={"test": True}, engine=eng)

    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        run = s.get(BacktestRunRow, run_id)
        assert run is not None
        assert run.symbol == "TEST"
        rows = s.query(TradeJournalRow).filter_by(backtest_run_id=run_id).all()
        assert len(rows) == summary.trade_count
        if rows:
            assert rows[0].mfe is not None
            assert rows[0].mae is not None
            assert rows[0].entry_reasons
            assert rows[0].exit_reasons


def test_stop_priority_over_target() -> None:
    """If a bar spans both stop and target, stop wins (conservative)."""

    # Build minimal series where after entry the next bar gaps through both levels.
    # Use a tiny custom strategy via monkeypatching evaluate_*.
    class OnceEntry(EmaTrendStub):
        version = "once@test"

        def warm_up(self) -> int:
            return 5

        def evaluate_entry(self, bars):  # type: ignore[no-untyped-def]
            from quant.backtesting.strategy import EntrySignal

            if len(bars) == 6:
                return EntrySignal(reasons=["test entry"], stop_distance_pct=0.02)
            return None

        def evaluate_exit(self, bars, entry_price):  # type: ignore[no-untyped-def]
            return None

    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    px = 100.0
    for i in range(8):
        if i == 6:
            # entry bar
            o = c = 100.0
            h, l = 100.5, 99.5
        elif i == 7:
            # both stop (~98.5 if atr) and target — force wide range
            o = 100.0
            h = 110.0
            l = 90.0
            c = 105.0
        else:
            o = c = px
            h, l = px + 0.2, px - 0.2
        bars.append(
            Bar(
                symbol="X",
                timeframe=Timeframe.D1,
                ts=start + timedelta(days=i),
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(l)),
                close=Decimal(str(c)),
                volume=Decimal(1000),
                source="t",
            )
        )
    # Frictionless model isolates the ordering rule from the cost model.
    engine = BacktestEngine(OnceEntry(), atr_stop_mult=1.0, target_rr=2.0, costs=CostModel.zero())
    summary = engine.run("X", Timeframe.D1, bars)
    assert summary.trade_count == 1
    trade = summary.trades[0]
    # wide bar hits stop first by engine rule
    assert ExitReason.STOP.value in trade.exit_reasons
    assert trade.exit == trade.stop
    assert trade.costs == Decimal(0)


def test_stop_fills_worse_than_trigger_with_costs() -> None:
    """A stop converts to a market order — it must never fill at the trigger price."""
    zero = BacktestEngine(
        _once_entry_strategy(), atr_stop_mult=1.0, target_rr=2.0, costs=CostModel.zero()
    ).run("X", Timeframe.D1, _stop_gap_bars())
    priced = BacktestEngine(_once_entry_strategy(), atr_stop_mult=1.0, target_rr=2.0).run(
        "X", Timeframe.D1, _stop_gap_bars()
    )

    assert zero.trade_count == priced.trade_count == 1
    slipped = priced.trades[0]
    assert slipped.exit < slipped.stop, "stop exit must slip below the trigger"
    assert slipped.entry > Decimal(100), "entry must pay the spread"
    assert slipped.costs > 0
    # Costs can only reduce realised P&L.
    assert priced.net_pnl < zero.net_pnl
    assert priced.gross_pnl > priced.net_pnl
    assert priced.total_costs == slipped.costs


def test_cost_model_is_deterministic() -> None:
    bars = _synthetic_uptrend(260)
    strategy_a = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=30, rsi_max=80)
    strategy_b = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=30, rsi_max=80)
    a = BacktestEngine(strategy_a).run("TEST", Timeframe.D1, bars)
    b = BacktestEngine(strategy_b).run("TEST", Timeframe.D1, bars)
    assert a.net_pnl == b.net_pnl
    assert a.total_costs == b.total_costs


def test_risk_adjusted_metrics_present() -> None:
    bars = _synthetic_uptrend(400)
    strategy = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=30, rsi_max=80)
    summary = BacktestEngine(strategy).run("TEST", Timeframe.D1, bars)
    assert summary.equity_curve
    assert summary.max_consecutive_losses >= 0
    assert summary.total_costs >= 0
    if summary.trade_count:
        assert summary.expectancy_usd is not None
