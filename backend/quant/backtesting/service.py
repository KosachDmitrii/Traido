"""
Evaluation service — the honest answer to "does this strategy make money?".

Ties the pieces together for one symbol: fetch history, run a cost-aware
backtest, run walk-forward so the headline number comes from data the
parameters never saw, compare against buy & hold, and break the result down by
market regime so a strategy that only works in one tape cannot hide.

Results are cached in memory. Nothing here touches the broker, and nothing here
can create an opportunity — it is a read-only measurement path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.enums import Timeframe
from core.ports import MarketDataPort
from core.schemas import Bar
from quant.backtesting.benchmark import buy_and_hold
from quant.backtesting.engine import BacktestEngine
from quant.backtesting.strategy import EmaTrendStub, Strategy
from quant.backtesting.walk_forward import OutOfSampleReport, walk_forward
from quant.costs import DEFAULT_COST_MODEL, CostModel
from quant.market_regime import segment_by_regime

DEFAULT_LOOKBACK_DAYS = 1500
DEFAULT_GRID: dict[str, list[Any]] = {
    "ema_fast": [20, 50],
    "ema_slow": [100, 200],
}
CACHE_TTL = timedelta(hours=12)
MIN_BARS = 300


class MarketDataUnavailable(RuntimeError):
    """History could not be fetched, so no evaluation can honestly be produced.

    Distinct from a ValueError about the symbol itself: this is an upstream
    outage the caller should retry, not bad input.
    """


def default_factory(params: Mapping[str, Any]) -> Strategy:
    return EmaTrendStub(
        ema_fast=int(params.get("ema_fast", 50)),
        ema_slow=int(params.get("ema_slow", 200)),
    )


@dataclass
class RegimeResult:
    regime: str
    bars: int
    trade_count: int
    return_pct: float
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float


@dataclass
class EvaluationResult:
    symbol: str
    timeframe: str
    generated_at: str
    bars: int

    # Full-sample backtest, after costs
    trade_count: int
    return_pct: float
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    cagr_pct: float | None
    expectancy_r: float | None
    total_costs: float
    gross_return_pct: float

    # Out of sample — the number that actually counts
    oos_trade_count: int
    oos_return_pct: float
    oos_win_rate: float
    oos_profit_factor: float | None
    oos_max_drawdown_pct: float
    oos_sharpe: float | None
    walk_forward_efficiency: float | None
    verdict: str

    # Benchmark
    benchmark_symbol: str
    benchmark_return_pct: float
    benchmark_max_drawdown_pct: float
    excess_return_pct: float
    beats_benchmark: bool

    by_regime: list[RegimeResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _CacheEntry:
    result: EvaluationResult
    at: datetime


_CACHE: dict[tuple[str, str], _CacheEntry] = {}


def clear_cache() -> None:
    _CACHE.clear()


async def evaluate_symbol(
    symbol: str,
    *,
    market_data: MarketDataPort,
    timeframe: Timeframe = Timeframe.D1,
    benchmark: str = "SPY",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    costs: CostModel | None = None,
    folds: int = 4,
    starting_equity: Decimal = Decimal(100000),
    use_cache: bool = True,
    now: datetime | None = None,
) -> EvaluationResult:
    symbol = symbol.upper()
    now = now or datetime.now(UTC)
    key = (symbol, timeframe.value)

    if use_cache:
        entry = _CACHE.get(key)
        if entry is not None and now - entry.at <= CACHE_TTL:
            return entry.result

    model = costs if costs is not None else DEFAULT_COST_MODEL
    start = now - timedelta(days=lookback_days)

    bars, bench_bars = await asyncio.gather(
        market_data.get_bars(symbol, timeframe, start, now),
        market_data.get_bars(benchmark, timeframe, start, now),
        return_exceptions=True,
    )
    if isinstance(bars, BaseException):
        raise MarketDataUnavailable(f"could not load history for {symbol}: {bars}") from bars
    # A missing benchmark only costs us the comparison, so degrade instead of failing.
    if isinstance(bench_bars, BaseException):
        bench_bars = []

    result = _evaluate(
        symbol,
        timeframe,
        bars,
        bench_bars,
        benchmark=benchmark,
        costs=model,
        folds=folds,
        starting_equity=starting_equity,
        now=now,
    )
    _CACHE[key] = _CacheEntry(result=result, at=now)
    return result


def _evaluate(
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    bench_bars: list[Bar],
    *,
    benchmark: str,
    costs: CostModel,
    folds: int,
    starting_equity: Decimal,
    now: datetime,
) -> EvaluationResult:
    warnings: list[str] = []
    if len(bars) < MIN_BARS:
        raise ValueError(f"{symbol}: {len(bars)} bars is not enough to evaluate (need {MIN_BARS})")

    strategy = default_factory({})
    summary = BacktestEngine(strategy, starting_equity=starting_equity, costs=costs).run(
        symbol, timeframe, bars
    )

    try:
        oos: OutOfSampleReport | None = walk_forward(
            default_factory,
            symbol,
            timeframe,
            bars,
            grid=DEFAULT_GRID,
            folds=folds,
            starting_equity=starting_equity,
            costs=costs,
        )
    except ValueError as exc:
        oos = None
        warnings.append(f"walk-forward skipped: {exc}")

    if oos is not None and not oos.trustworthy:
        warnings.append(
            f"Only {oos.trade_count} out-of-sample trades — treat these numbers as indicative"
        )

    warm = strategy.warm_up()
    if bench_bars:
        bench = buy_and_hold(
            benchmark,
            timeframe,
            bench_bars,
            starting_equity=starting_equity,
            costs=costs,
            warm_up=min(warm, max(0, len(bench_bars) - 2)),
        )
        bench_return, bench_dd = bench.return_pct, bench.max_drawdown_pct
    else:
        warnings.append(f"No {benchmark} history — benchmark comparison unavailable")
        bench_return, bench_dd = 0.0, 0.0

    oos_return = oos.return_pct if oos else 0.0
    excess = oos_return - bench_return

    return EvaluationResult(
        symbol=symbol,
        timeframe=timeframe.value,
        generated_at=now.isoformat(),
        bars=len(bars),
        trade_count=summary.trade_count,
        return_pct=summary.return_pct,
        win_rate=summary.win_rate,
        profit_factor=summary.profit_factor,
        max_drawdown_pct=summary.max_drawdown_pct,
        sharpe=summary.sharpe,
        sortino=summary.sortino,
        calmar=summary.calmar,
        cagr_pct=summary.cagr_pct,
        expectancy_r=summary.expectancy_r,
        total_costs=float(summary.total_costs),
        gross_return_pct=float(summary.gross_pnl / starting_equity * 100),
        oos_trade_count=oos.trade_count if oos else 0,
        oos_return_pct=oos_return,
        oos_win_rate=oos.win_rate if oos else 0.0,
        oos_profit_factor=oos.profit_factor if oos else None,
        oos_max_drawdown_pct=oos.max_drawdown_pct if oos else 0.0,
        oos_sharpe=oos.sharpe if oos else None,
        walk_forward_efficiency=oos.walk_forward_efficiency if oos else None,
        verdict=oos.verdict() if oos else "NOT_EVALUATED",
        benchmark_symbol=benchmark.upper(),
        benchmark_return_pct=bench_return,
        benchmark_max_drawdown_pct=bench_dd,
        excess_return_pct=excess,
        beats_benchmark=excess > 0,
        by_regime=_by_regime(symbol, timeframe, bars, costs, starting_equity),
        warnings=warnings,
    )


def _by_regime(
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    costs: CostModel,
    starting_equity: Decimal,
) -> list[RegimeResult]:
    """Score the strategy separately in each regime it lived through."""
    out: list[RegimeResult] = []
    for segment in segment_by_regime(bars):
        strategy = default_factory({})
        if segment.length < strategy.warm_up() + 10:
            continue
        try:
            summary = BacktestEngine(strategy, starting_equity=starting_equity, costs=costs).run(
                symbol, timeframe, segment.bars
            )
        except ValueError:
            continue
        out.append(
            RegimeResult(
                regime=segment.label.value,
                bars=segment.length,
                trade_count=summary.trade_count,
                return_pct=summary.return_pct,
                win_rate=summary.win_rate,
                profit_factor=summary.profit_factor,
                max_drawdown_pct=summary.max_drawdown_pct,
            )
        )
    return out
