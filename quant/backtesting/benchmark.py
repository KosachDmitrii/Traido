"""
Buy & hold benchmark over the same bars, priced with the same cost model.

A strategy that returns less than buy & hold at comparable risk has no reason
to exist. Every evaluation in Traido reports this side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.enums import Timeframe
from core.schemas import Bar
from quant.backtesting.metrics import (
    cagr_pct,
    max_drawdown_pct,
    sharpe_ratio,
    sortino_ratio,
)
from quant.costs import DEFAULT_COST_MODEL, CostModel, FillKind


@dataclass(frozen=True)
class BenchmarkResult:
    symbol: str
    timeframe: Timeframe
    starting_equity: Decimal
    ending_equity: Decimal
    return_pct: float
    max_drawdown_pct: float
    sharpe: float | None
    sortino: float | None
    cagr_pct: float | None
    equity_curve: list[float]


def buy_and_hold(
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    *,
    starting_equity: Decimal = Decimal(100000),
    costs: CostModel | None = None,
    warm_up: int = 0,
) -> BenchmarkResult:
    """
    Buy at the first usable bar, hold to the last, sell.

    `warm_up` should match the strategy's warm-up so both curves cover the same
    period — otherwise the comparison is not apples to apples.
    """
    model = costs if costs is not None else DEFAULT_COST_MODEL
    usable = bars[warm_up:] if warm_up < len(bars) else []
    if len(usable) < 2:
        return BenchmarkResult(
            symbol=symbol.upper(),
            timeframe=timeframe,
            starting_equity=starting_equity,
            ending_equity=starting_equity,
            return_pct=0.0,
            max_drawdown_pct=0.0,
            sharpe=None,
            sortino=None,
            cagr_pct=None,
            equity_curve=[float(starting_equity)],
        )

    entry_ref = Decimal(str(usable[0].close))
    entry = model.fill_price(entry_ref, side="buy", kind=FillKind.MARKET)
    qty = (starting_equity / entry).quantize(Decimal("0.0001"))
    entry_fees = model.fees(qty, entry, side="buy")
    cash = starting_equity - qty * entry - entry_fees

    curve: list[float] = [float(starting_equity)]
    for bar in usable[1:]:
        curve.append(float(cash + qty * Decimal(str(bar.close))))

    exit_ref = Decimal(str(usable[-1].close))
    exit_price = model.fill_price(exit_ref, side="sell", kind=FillKind.MARKET)
    exit_fees = model.fees(qty, exit_price, side="sell")
    ending = cash + qty * exit_price - exit_fees
    curve[-1] = float(ending)

    return BenchmarkResult(
        symbol=symbol.upper(),
        timeframe=timeframe,
        starting_equity=starting_equity,
        ending_equity=ending.quantize(Decimal("0.01")),
        return_pct=float((ending - starting_equity) / starting_equity * 100),
        max_drawdown_pct=max_drawdown_pct(curve),
        sharpe=sharpe_ratio(curve, timeframe),
        sortino=sortino_ratio(curve, timeframe),
        cagr_pct=cagr_pct(curve, timeframe),
        equity_curve=curve,
    )
