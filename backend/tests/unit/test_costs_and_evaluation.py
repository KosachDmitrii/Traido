"""Cost model, risk-adjusted metrics, benchmark, and out-of-sample evaluation."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.enums import Timeframe
from core.schemas import Bar
from quant.backtesting.benchmark import buy_and_hold
from quant.backtesting.engine import BacktestEngine
from quant.backtesting.metrics import (
    cagr_pct,
    calmar_ratio,
    max_consecutive_losses,
    period_returns,
    sharpe_ratio,
    sortino_ratio,
)
from quant.backtesting.strategy import EmaTrendStub
from quant.backtesting.walk_forward import (
    robust_objective,
    train_test_split,
    walk_forward,
)
from quant.costs import CostModel, FillKind


def _bars(n: int, *, drift: float = 0.0006, swing: float = 0.05, period: int = 40) -> list[Bar]:
    """
    Trending series with regular pullbacks.

    A monotone ramp is useless as a fixture: RSI pins near 100 and a trend
    strategy never finds an entry. The sine component produces the pullbacks
    that real trends have, so entries and exits actually fire.
    """
    bars: list[Bar] = []
    start = datetime(2022, 1, 3, 14, 30, tzinfo=UTC)
    prev = 100.0
    for i in range(n):
        trend = 100.0 * ((1.0 + drift) ** i)
        c = trend * (1.0 + swing * math.sin(2 * math.pi * i / period))
        o = prev
        h = max(o, c) * 1.004
        l = min(o, c) * 0.996
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
        prev = c
    return bars


# ── Cost model ───────────────────────────────────────────────────────────────


def test_fills_always_move_against_the_trader() -> None:
    m = CostModel()
    ref = Decimal(100)
    assert m.fill_price(ref, side="buy", kind=FillKind.MARKET) > ref
    assert m.fill_price(ref, side="sell", kind=FillKind.MARKET) < ref


def test_stop_fills_worse_than_market_worse_than_limit() -> None:
    m = CostModel()
    ref = Decimal(100)
    limit = m.fill_price(ref, side="sell", kind=FillKind.LIMIT)
    market = m.fill_price(ref, side="sell", kind=FillKind.MARKET)
    stop = m.fill_price(ref, side="sell", kind=FillKind.STOP)
    assert stop < market < limit


def test_regulatory_fees_are_sell_side_only() -> None:
    m = CostModel()
    qty, price = Decimal(1000), Decimal(50)
    assert m.fees(qty, price, side="buy") == Decimal(0)
    assert m.fees(qty, price, side="sell") > Decimal(0)


def test_finra_taf_is_capped() -> None:
    m = CostModel(sec_fee_rate=Decimal(0))
    huge = m.fees(Decimal(10000000), Decimal(10), side="sell")
    assert huge == m.finra_taf_max


def test_zero_model_is_frictionless() -> None:
    m = CostModel.zero()
    ref = Decimal(100)
    assert m.fill_price(ref, side="buy", kind=FillKind.STOP) == ref
    assert m.fees(Decimal(100), ref, side="sell") == Decimal(0)
    assert m.round_trip_cost_bps(ref, Decimal(100)) == 0.0


def test_conservative_model_costs_more_than_default() -> None:
    price, qty = Decimal(100), Decimal(100)
    assert CostModel.conservative().round_trip_cost_bps(
        price, qty
    ) > CostModel().round_trip_cost_bps(price, qty)


# ── Metrics ──────────────────────────────────────────────────────────────────


def test_period_returns_skips_non_positive_equity() -> None:
    assert period_returns([100.0, 110.0]) == pytest.approx([0.1])
    assert period_returns([0.0, 110.0]) == []


def test_sharpe_is_none_without_variance() -> None:
    flat = [100.0] * 50
    assert sharpe_ratio(flat, Timeframe.D1) is None


def test_sharpe_positive_for_rising_noisy_curve() -> None:
    curve = [100.0 * (1.001**i) * (1 + 0.002 * ((i % 5) - 2)) for i in range(300)]
    s = sharpe_ratio(curve, Timeframe.D1)
    assert s is not None and s > 0


def test_sortino_ignores_upside_volatility() -> None:
    steady = [100.0 * (1.001**i) for i in range(200)]
    assert sortino_ratio(steady, Timeframe.D1) is None  # no downside at all


def test_cagr_matches_known_doubling() -> None:
    curve = [100.0 * (2 ** (i / 252)) for i in range(253)]
    assert cagr_pct(curve, Timeframe.D1) == pytest.approx(100.0, abs=1.0)


def test_calmar_needs_a_drawdown() -> None:
    monotone = [100.0 + i for i in range(300)]
    assert calmar_ratio(monotone, Timeframe.D1) is None


def test_max_consecutive_losses_counts_the_worst_run() -> None:
    class _T:
        def __init__(self, pnl: float) -> None:
            self.pnl = Decimal(str(pnl))

    trades = [_T(1), _T(-1), _T(-1), _T(-1), _T(2), _T(-1)]
    assert max_consecutive_losses(trades) == 3  # type: ignore[arg-type]


# ── Engine integration ───────────────────────────────────────────────────────


def test_costs_reduce_returns_monotonically() -> None:
    bars = _bars(400)

    def run(model: CostModel) -> Decimal:
        strategy = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=30, rsi_max=80)
        return BacktestEngine(strategy, costs=model).run("TEST", Timeframe.D1, bars).net_pnl

    free = run(CostModel.zero())
    normal = run(CostModel())
    harsh = run(CostModel.conservative())
    assert free > normal > harsh


def test_summary_reports_gross_net_and_costs_consistently() -> None:
    bars = _bars(400)
    strategy = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=30, rsi_max=80)
    s = BacktestEngine(strategy).run("TEST", Timeframe.D1, bars)
    assert s.trade_count > 0, "fixture must generate trades for this to mean anything"
    assert s.total_costs > 0
    assert float(s.gross_pnl) == pytest.approx(float(s.net_pnl + s.total_costs), abs=0.02)
    assert len(s.equity_curve) > 1


def test_equity_curve_never_goes_negative() -> None:
    bars = _bars(400, drift=-0.001)
    strategy = EmaTrendStub(ema_fast=20, ema_slow=50, rsi_min=10, rsi_max=90)
    s = BacktestEngine(strategy).run("TEST", Timeframe.D1, bars)
    assert all(v > 0 for v in s.equity_curve)


# ── Benchmark ────────────────────────────────────────────────────────────────


def test_buy_and_hold_tracks_the_underlying() -> None:
    bars = _bars(300)
    result = buy_and_hold("TEST", Timeframe.D1, bars, warm_up=55)
    first = float(bars[55].close)
    last = float(bars[-1].close)
    expected = (last - first) / first * 100
    # Within costs of the raw move.
    assert result.return_pct == pytest.approx(expected, abs=1.0)
    assert result.return_pct < expected, "benchmark must also pay costs"


def test_buy_and_hold_handles_short_series() -> None:
    result = buy_and_hold("TEST", Timeframe.D1, _bars(5), warm_up=10)
    assert result.return_pct == 0.0
    assert result.equity_curve == [100000.0]


# ── Walk-forward ─────────────────────────────────────────────────────────────


def _factory(params):  # type: ignore[no-untyped-def]
    return EmaTrendStub(
        ema_fast=params.get("ema_fast", 20),
        ema_slow=params.get("ema_slow", 50),
        rsi_min=30,
        rsi_max=80,
    )


def test_robust_objective_rejects_thin_samples() -> None:
    class _S:
        trade_count = 2
        expectancy_r = 5.0

    assert robust_objective(_S()) == float("-inf")  # type: ignore[arg-type]


def test_robust_objective_prefers_more_trades_at_equal_edge() -> None:
    class _S:
        def __init__(self, n: int) -> None:
            self.trade_count = n
            self.expectancy_r = 0.3

    assert robust_objective(_S(100)) > robust_objective(_S(25))  # type: ignore[arg-type]


def test_train_test_split_scores_only_the_holdout() -> None:
    bars = _bars(700)
    report = train_test_split(
        _factory,
        "TEST",
        Timeframe.D1,
        bars,
        grid={"ema_fast": [10, 20]},
        train_fraction=0.6,
    )
    assert len(report.folds) == 1
    fold = report.folds[0]
    assert fold.train_range == (0, 420)
    assert fold.test_range == (420, 700)
    assert fold.params["ema_fast"] in (10, 20)


def test_walk_forward_windows_never_overlap_train_and_test() -> None:
    bars = _bars(1000)
    report = walk_forward(_factory, "TEST", Timeframe.D1, bars, folds=4)
    assert len(report.folds) == 4
    for fold in report.folds:
        assert fold.train_range[1] <= fold.test_range[0], "train must end before test begins"
        assert fold.test_range[0] < fold.test_range[1]


def test_walk_forward_anchored_expands_the_train_window() -> None:
    bars = _bars(1000)
    report = walk_forward(_factory, "TEST", Timeframe.D1, bars, folds=3, anchored=True)
    for fold in report.folds:
        assert fold.train_range[0] == 0


def test_walk_forward_rejects_impossible_fold_counts() -> None:
    with pytest.raises(ValueError, match="not enough bars"):
        walk_forward(_factory, "TEST", Timeframe.D1, _bars(60), folds=50)


def test_report_verdict_flags_thin_samples() -> None:
    bars = _bars(600)
    report = walk_forward(_factory, "TEST", Timeframe.D1, bars, folds=3)
    if not report.trustworthy:
        assert "INSUFFICIENT_SAMPLE" in report.verdict()
    else:
        assert report.verdict() in {
            "PASS",
            "FAIL_NEGATIVE_OOS",
            "FAIL_WEAK_PROFIT_FACTOR",
            "FAIL_OVERFIT",
        }


def test_stitched_curve_compounds_across_folds() -> None:
    bars = _bars(1000)
    report = walk_forward(_factory, "TEST", Timeframe.D1, bars, folds=4)
    assert report.equity_curve[0] == 100000.0
    implied = (report.equity_curve[-1] - 100000.0) / 100000.0 * 100
    assert report.return_pct == pytest.approx(implied)
    assert all(math.isfinite(v) for v in report.equity_curve)
