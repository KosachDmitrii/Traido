"""
Walk-forward and out-of-sample evaluation.

A single backtest over one period proves nothing: parameters can always be bent
until the curve looks good. These helpers separate the data used to choose
parameters from the data used to judge them.

- `train_test_split` — one in-sample fit, one untouched holdout.
- `walk_forward`     — rolling anchored/unanchored folds where each fold picks
                       parameters on its train window and is scored only on the
                       following, unseen test window. The stitched test curve is
                       the number worth trusting.

Walk-forward efficiency (WFE) = out-of-sample return / in-sample return.
Below ~0.5 the parameters are fitting noise.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.enums import Timeframe
from core.schemas import BacktestSummary, BacktestTrade, Bar
from quant.backtesting.engine import BacktestEngine
from quant.backtesting.metrics import (
    cagr_pct,
    max_drawdown_pct,
    period_returns,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)
from quant.backtesting.strategy import Strategy
from quant.costs import DEFAULT_COST_MODEL, CostModel

StrategyFactory = Callable[[Mapping[str, Any]], Strategy]
Objective = Callable[[BacktestSummary], float]

MIN_TRADES_FOR_TRUST = 20
"""Fewer closed trades than this and the statistics are anecdotes."""


def robust_objective(summary: BacktestSummary, min_trades: int = 5) -> float:
    """
    Expectancy weighted by sample size.

    Prefers a modest edge repeated many times over a spectacular edge seen
    three times. Returns -inf below `min_trades` so thin fits never win.
    """
    if summary.trade_count < min_trades:
        return float("-inf")
    exp_r = summary.expectancy_r
    if exp_r is None:
        return float("-inf")
    return exp_r * math.sqrt(summary.trade_count)


@dataclass(frozen=True)
class FoldResult:
    index: int
    train_range: tuple[int, int]
    test_range: tuple[int, int]
    params: dict[str, Any]
    train: BacktestSummary | None
    test: BacktestSummary | None
    note: str = ""


@dataclass(frozen=True)
class OutOfSampleReport:
    """Aggregate of everything the strategy was never fitted on."""

    strategy_version: str
    symbol: str
    timeframe: Timeframe
    folds: list[FoldResult] = field(default_factory=list)

    trade_count: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float | None = None
    sortino: float | None = None
    cagr_pct: float | None = None
    total_costs: Decimal = Decimal(0)
    equity_curve: list[float] = field(default_factory=list)
    walk_forward_efficiency: float | None = None

    @property
    def trustworthy(self) -> bool:
        """Enough out-of-sample trades to say anything at all."""
        return self.trade_count >= MIN_TRADES_FOR_TRUST

    def verdict(self) -> str:
        if not self.trustworthy:
            return f"INSUFFICIENT_SAMPLE ({self.trade_count} OOS trades)"
        if self.return_pct <= 0:
            return "FAIL_NEGATIVE_OOS"
        if self.profit_factor is not None and self.profit_factor < 1.2:
            return "FAIL_WEAK_PROFIT_FACTOR"
        if self.walk_forward_efficiency is not None and self.walk_forward_efficiency < 0.4:
            return "FAIL_OVERFIT"
        return "PASS"


def _param_grid(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = list(grid.keys())
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*grid.values())]


def _run(
    factory: StrategyFactory,
    params: Mapping[str, Any],
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    *,
    starting_equity: Decimal,
    costs: CostModel,
    risk_per_trade_pct: float,
) -> BacktestSummary | None:
    strategy = factory(params)
    if len(bars) < strategy.warm_up() + 2:
        return None
    engine = BacktestEngine(
        strategy,
        starting_equity=starting_equity,
        risk_per_trade_pct=risk_per_trade_pct,
        costs=costs,
    )
    try:
        return engine.run(symbol, timeframe, bars)
    except ValueError:
        return None


def train_test_split(
    factory: StrategyFactory,
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    *,
    grid: Mapping[str, Sequence[Any]] | None = None,
    train_fraction: float = 0.6,
    starting_equity: Decimal = Decimal(100000),
    costs: CostModel | None = None,
    risk_per_trade_pct: float = 1.0,
    objective: Objective = robust_objective,
) -> OutOfSampleReport:
    """Fit on the first `train_fraction` of bars, judge on the rest."""
    if not 0.2 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be in [0.2, 0.9]")
    model = costs if costs is not None else DEFAULT_COST_MODEL
    split = int(len(bars) * train_fraction)
    folds = _evaluate_folds(
        factory,
        symbol,
        timeframe,
        bars,
        [((0, split), (split, len(bars)))],
        grid=grid or {},
        starting_equity=starting_equity,
        costs=model,
        risk_per_trade_pct=risk_per_trade_pct,
        objective=objective,
    )
    return _aggregate(factory, symbol, timeframe, folds, starting_equity)


def walk_forward(
    factory: StrategyFactory,
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    *,
    grid: Mapping[str, Sequence[Any]] | None = None,
    folds: int = 4,
    train_bars: int | None = None,
    anchored: bool = False,
    starting_equity: Decimal = Decimal(100000),
    costs: CostModel | None = None,
    risk_per_trade_pct: float = 1.0,
    objective: Objective = robust_objective,
) -> OutOfSampleReport:
    """
    Rolling walk-forward.

    `anchored=True` keeps the train window growing from bar 0 (expanding
    window); otherwise the train window slides at fixed length.
    """
    if folds < 1:
        raise ValueError("folds must be >= 1")
    model = costs if costs is not None else DEFAULT_COST_MODEL

    total = len(bars)
    test_len = total // (folds + 1)
    if test_len < 2:
        raise ValueError("not enough bars for the requested number of folds")
    train_len = train_bars if train_bars is not None else test_len

    windows: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for k in range(folds):
        test_start = total - (folds - k) * test_len
        test_end = test_start + test_len
        train_start = 0 if anchored else max(0, test_start - train_len)
        if test_start - train_start < 2:
            continue
        windows.append(((train_start, test_start), (test_start, test_end)))

    fold_results = _evaluate_folds(
        factory,
        symbol,
        timeframe,
        bars,
        windows,
        grid=grid or {},
        starting_equity=starting_equity,
        costs=model,
        risk_per_trade_pct=risk_per_trade_pct,
        objective=objective,
    )
    return _aggregate(factory, symbol, timeframe, fold_results, starting_equity)


def _evaluate_folds(
    factory: StrategyFactory,
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    windows: Sequence[tuple[tuple[int, int], tuple[int, int]]],
    *,
    grid: Mapping[str, Sequence[Any]],
    starting_equity: Decimal,
    costs: CostModel,
    risk_per_trade_pct: float,
    objective: Objective,
) -> list[FoldResult]:
    combos = _param_grid(grid)
    results: list[FoldResult] = []

    for idx, ((tr_a, tr_b), (te_a, te_b)) in enumerate(windows):
        train_bars_slice = bars[tr_a:tr_b]
        best_params: dict[str, Any] = combos[0]
        best_summary: BacktestSummary | None = None
        best_score = float("-inf")

        for params in combos:
            summary = _run(
                factory,
                params,
                symbol,
                timeframe,
                train_bars_slice,
                starting_equity=starting_equity,
                costs=costs,
                risk_per_trade_pct=risk_per_trade_pct,
            )
            if summary is None:
                continue
            score = objective(summary)
            if score > best_score:
                best_score, best_params, best_summary = score, params, summary

        # Test window carries the strategy's warm-up with it so indicators are
        # seeded from history rather than starting cold inside the fold.
        warm = factory(best_params).warm_up()
        test_slice = bars[max(0, te_a - warm) : te_b]
        test_summary = _run(
            factory,
            best_params,
            symbol,
            timeframe,
            test_slice,
            starting_equity=starting_equity,
            costs=costs,
            risk_per_trade_pct=risk_per_trade_pct,
        )

        note = ""
        if best_summary is None:
            note = "no viable parameter set on train window"
        elif test_summary is None:
            note = "test window too short after warm-up"

        results.append(
            FoldResult(
                index=idx,
                train_range=(tr_a, tr_b),
                test_range=(te_a, te_b),
                params=dict(best_params),
                train=best_summary,
                test=test_summary,
                note=note,
            )
        )
    return results


def _aggregate(
    factory: StrategyFactory,
    symbol: str,
    timeframe: Timeframe,
    folds: list[FoldResult],
    starting_equity: Decimal,
) -> OutOfSampleReport:
    tests = [f.test for f in folds if f.test is not None]
    version = factory({}).version

    if not tests:
        return OutOfSampleReport(
            strategy_version=version,
            symbol=symbol.upper(),
            timeframe=timeframe,
            folds=folds,
            equity_curve=[float(starting_equity)],
        )

    trades: list[BacktestTrade] = [t for s in tests for t in s.trades]

    # Stitch fold curves by compounding their period returns onto one account.
    stitched: list[float] = [float(starting_equity)]
    for summary in tests:
        for r in period_returns(summary.equity_curve):
            stitched.append(stitched[-1] * (1.0 + r))

    ending = stitched[-1]
    ret_pct = (ending - float(starting_equity)) / float(starting_equity) * 100.0

    is_returns = [f.train.return_pct for f in folds if f.train is not None]
    oos_returns = [f.test.return_pct for f in folds if f.test is not None]
    wfe: float | None = None
    if is_returns and oos_returns:
        is_avg = sum(is_returns) / len(is_returns)
        oos_avg = sum(oos_returns) / len(oos_returns)
        if is_avg > 0:
            wfe = oos_avg / is_avg

    pf = profit_factor(trades)
    return OutOfSampleReport(
        strategy_version=version,
        symbol=symbol.upper(),
        timeframe=timeframe,
        folds=folds,
        trade_count=len(trades),
        win_rate=win_rate(trades),
        profit_factor=999.0 if pf == float("inf") else pf,
        return_pct=ret_pct,
        max_drawdown_pct=max_drawdown_pct(stitched),
        sharpe=sharpe_ratio(stitched, timeframe),
        sortino=sortino_ratio(stitched, timeframe),
        cagr_pct=cagr_pct(stitched, timeframe),
        total_costs=sum((t.costs for t in trades), Decimal(0)),
        equity_curve=stitched,
        walk_forward_efficiency=wfe,
    )
