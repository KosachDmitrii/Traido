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
from quant.backtesting.desk_strategy import DeskConfluenceStrategy
from quant.backtesting.engine import BacktestEngine
from quant.backtesting.strategy import EmaTrendStub, Strategy
from quant.backtesting.walk_forward import OutOfSampleReport, walk_forward
from quant.costs import DEFAULT_COST_MODEL, CostModel
from quant.market_regime import segment_by_regime

DEFAULT_LOOKBACK_DAYS = 1500
# Research stub grid (ema_trend_stub).
STUB_GRID: dict[str, list[Any]] = {
    "ema_fast": [20, 50],
    "ema_slow": [100, 200],
}
# Desk geometry knobs — small grid so walk-forward still has a choice.
DESK_GRID: dict[str, list[Any]] = {
    "rsi_cap": [68.0, 72.0],
    "near_sma_frac": [0.02, 0.025],
}
# Backward-compatible alias used by older tests / callers.
DEFAULT_GRID = STUB_GRID
CACHE_TTL = timedelta(hours=12)
MIN_BARS = 300

StrategyKind = str  # "desk" | "stub"


class MarketDataUnavailable(RuntimeError):
    """History could not be fetched, so no evaluation can honestly be produced.

    Distinct from a ValueError about the symbol itself: this is an upstream
    outage the caller should retry, not bad input.
    """


def desk_factory(params: Mapping[str, Any]) -> Strategy:
    """Bar adapter for trader_desk — same version key the desk stamps on paper/live."""
    return DeskConfluenceStrategy(
        rsi_cap=float(params.get("rsi_cap", 72.0)),
        near_sma_frac=float(params.get("near_sma_frac", 0.025)),
        chase_ext_frac=float(params.get("chase_ext_frac", 0.04)),
    )


def stub_factory(params: Mapping[str, Any]) -> Strategy:
    return EmaTrendStub(
        ema_fast=int(params.get("ema_fast", 50)),
        ema_slow=int(params.get("ema_slow", 200)),
    )


def default_factory(params: Mapping[str, Any]) -> Strategy:
    """Default Evaluation path = desk strategy (Stage 8 identity with the desk)."""
    return desk_factory(params)


def resolve_strategy_kind(kind: StrategyKind | None) -> tuple[StrategyKind, Any, dict[str, list[Any]]]:
    """Return (kind, factory, walk-forward grid)."""
    resolved = (kind or "desk").strip().lower()
    if resolved in {"stub", "ema", "ema_trend_stub", "research"}:
        return "stub", stub_factory, STUB_GRID
    if resolved in {"desk", "trader_desk", "live", "confluence"}:
        return "desk", desk_factory, DESK_GRID
    raise ValueError(f"unknown evaluation strategy kind: {kind!r} (use desk|stub)")


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
    strategy_version: str

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
        data = asdict(self)
        # Nested shape the promotion gate reads for OOS metrics.
        data["out_of_sample"] = {
            "trade_count": self.oos_trade_count,
            "return_pct": self.oos_return_pct,
            "win_rate": self.oos_win_rate,
            "profit_factor": self.oos_profit_factor,
            "max_drawdown_pct": self.oos_max_drawdown_pct,
            "sharpe": self.oos_sharpe,
            "walk_forward_efficiency": self.walk_forward_efficiency,
            "verdict": self.verdict,
        }
        return data



@dataclass
class _CacheEntry:
    result: EvaluationResult
    at: datetime


_CACHE: dict[tuple[str, str, str], _CacheEntry] = {}


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
    strategy: StrategyKind = "desk",
) -> EvaluationResult:
    symbol = symbol.upper()
    now = now or datetime.now(UTC)
    kind, factory, grid = resolve_strategy_kind(strategy)
    key = (symbol, timeframe.value, kind)

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
        factory=factory,
        grid=grid,
    )
    _CACHE[key] = _CacheEntry(result=result, at=now)
    _persist_evaluation_evidence(result)
    return result


def _persist_evaluation_evidence(result: EvaluationResult) -> None:
    """Stage 8: attach Evaluation evidence to whatever version was measured."""
    try:
        from datetime import datetime

        from core.enums import Timeframe as Tf
        from core.schemas import BacktestSummary
        from database.repository import persist_backtest_summary
        from strategy.promotion import persist_evaluation_run
        from strategy.registry import ensure_builtin_strategies

        ensure_builtin_strategies()
        payload = result.as_dict()
        persist_evaluation_run(
            strategy_version_key=result.strategy_version,
            symbol=result.symbol,
            timeframe=result.timeframe,
            verdict=result.verdict,
            payload=payload,
            generated_at=datetime.fromisoformat(result.generated_at),
        )
        starting = Decimal(100000)
        net = starting * Decimal(str(result.return_pct)) / Decimal(100)
        wins = round(result.win_rate * result.trade_count)
        summary = BacktestSummary(
            strategy_version=result.strategy_version,
            symbol=result.symbol,
            timeframe=Tf(result.timeframe),
            starting_equity=starting,
            ending_equity=starting + net,
            net_pnl=net,
            return_pct=result.return_pct,
            trade_count=result.trade_count,
            win_count=wins,
            loss_count=max(0, result.trade_count - wins),
            win_rate=result.win_rate,
            profit_factor=result.profit_factor,
            max_drawdown_pct=result.max_drawdown_pct,
            avg_r=result.expectancy_r,
            avg_bars_held=None,
            total_costs=Decimal(str(result.total_costs)),
            expectancy_r=result.expectancy_r,
            sharpe=result.sharpe,
            sortino=result.sortino,
            calmar=result.calmar,
            cagr_pct=result.cagr_pct,
        )
        persist_backtest_summary(
            summary,
            params={"source": "evaluation"},
            notes="evaluation full-sample (Stage 8 evidence)",
        )
    except Exception:  # noqa: BLE001, S110 — measurement must not break the read path
        pass


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
    factory: Any = None,
    grid: dict[str, list[Any]] | None = None,
) -> EvaluationResult:
    warnings: list[str] = []
    if len(bars) < MIN_BARS:
        raise ValueError(f"{symbol}: {len(bars)} bars is not enough to evaluate (need {MIN_BARS})")

    factory = factory or default_factory
    grid = grid if grid is not None else DESK_GRID
    strategy = factory({})
    from strategy.registry import LIVE_STRATEGY_KEY, RESEARCH_STRATEGY_KEY

    fallback_key = (
        LIVE_STRATEGY_KEY
        if getattr(strategy, "version", "").startswith("trader_desk")
        else RESEARCH_STRATEGY_KEY
    )

    summary = BacktestEngine(strategy, starting_equity=starting_equity, costs=costs).run(
        symbol, timeframe, bars
    )

    try:
        oos: OutOfSampleReport | None = walk_forward(
            factory,
            symbol,
            timeframe,
            bars,
            grid=grid,
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
        strategy_version=getattr(strategy, "version", None) or fallback_key,
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
        by_regime=_by_regime(
            symbol, timeframe, bars, costs, starting_equity, factory=factory
        ),
        warnings=warnings,
    )


def _by_regime(
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    costs: CostModel,
    starting_equity: Decimal,
    *,
    factory: Any = None,
) -> list[RegimeResult]:
    """Score the strategy separately in each regime it lived through."""
    factory = factory or default_factory
    out: list[RegimeResult] = []
    for segment in segment_by_regime(bars):
        strategy = factory({})
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
