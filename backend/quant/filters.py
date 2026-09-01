"""
Tradability filters.

Everything upstream of this module is trying to find reasons to trade. This
module's only job is to find reasons not to. A setup passes only if it clears
every gate, and the gates are deliberately blunt:

- Illiquid names cannot be exited at the modelled price.
- Sub-$5 stocks have spreads that eat any realistic edge.
- A target inside the round-trip cost is not an edge, it is a fee generator.
- Dead-flat names have no room to reach a 2R target before the thesis expires.
- Stale bars mean we are trading a price that no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.schemas import Bar
from quant.costs import DEFAULT_COST_MODEL, CostModel
from quant.volatility import average_dollar_volume, compute_volatility


@dataclass(frozen=True)
class TradabilityLimits:
    min_price: Decimal = Decimal(5)
    max_price: Decimal = Decimal(10000)
    min_avg_dollar_volume: float = 20_000_000.0
    """20M/day keeps us in names where a retail-sized order is invisible."""
    min_atr_pct: float = 0.75
    """Below this the symbol cannot travel far enough to pay for the trade."""
    max_atr_pct: float = 12.0
    """Above this, stops are so wide that position size collapses to noise."""
    min_edge_to_cost_ratio: float = 5.0
    """Target distance must be at least this many times the round-trip friction."""
    max_bar_staleness: timedelta = timedelta(days=5)
    min_bars: int = 60


@dataclass(frozen=True)
class TradabilityResult:
    symbol: str
    passed: bool
    rejections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    avg_dollar_volume: float | None = None
    atr_pct: float | None = None
    edge_to_cost_ratio: float | None = None


def check_tradability(
    symbol: str,
    bars: list[Bar],
    *,
    limits: TradabilityLimits | None = None,
    costs: CostModel | None = None,
    target: Decimal | None = None,
    stop: Decimal | None = None,
    now: datetime | None = None,
) -> TradabilityResult:
    """
    Gate a symbol before it can become a candidate.

    `target`/`stop` are optional: when supplied, the edge-versus-cost test runs
    against the actual planned trade geometry rather than a generic estimate.
    """
    lim = limits or TradabilityLimits()
    model = costs if costs is not None else DEFAULT_COST_MODEL
    now = now or datetime.now(UTC)
    rejections: list[str] = []
    notes: list[str] = []

    if len(bars) < lim.min_bars:
        return TradabilityResult(
            symbol=symbol.upper(),
            passed=False,
            rejections=["INSUFFICIENT_HISTORY"],
            notes=[f"{len(bars)} bars, need {lim.min_bars}"],
        )

    last_bar = bars[-1]
    price = Decimal(str(last_bar.close))

    if price < lim.min_price:
        rejections.append("PRICE_TOO_LOW")
    if price > lim.max_price:
        rejections.append("PRICE_TOO_HIGH")

    age = now - last_bar.ts
    if age > lim.max_bar_staleness:
        rejections.append("STALE_DATA")
        notes.append(f"Last bar is {age.days}d old")

    adv = average_dollar_volume(bars, 20)
    if adv is not None and adv < lim.min_avg_dollar_volume:
        rejections.append("INSUFFICIENT_LIQUIDITY")
        notes.append(f"ADV ${adv / 1e6:.1f}M below ${lim.min_avg_dollar_volume / 1e6:.0f}M floor")

    vol = compute_volatility(bars)
    atr_pct = vol.atr_pct
    if atr_pct is not None:
        if atr_pct < lim.min_atr_pct:
            rejections.append("VOLATILITY_TOO_LOW")
            notes.append(f"ATR {atr_pct:.2f}% leaves no room to a target")
        elif atr_pct > lim.max_atr_pct:
            rejections.append("VOLATILITY_TOO_HIGH")
            notes.append(f"ATR {atr_pct:.2f}% forces an unusably wide stop")

    edge_ratio: float | None = None
    if target is not None and price > 0:
        move_bps = float((target - price) / price) * 10_000
        cost_bps = model.round_trip_cost_bps(price, Decimal(100))
        if cost_bps > 0:
            edge_ratio = move_bps / cost_bps
            if edge_ratio < lim.min_edge_to_cost_ratio:
                rejections.append("EDGE_BELOW_COST_THRESHOLD")
                notes.append(
                    f"Target is {edge_ratio:.1f}x round-trip cost, need "
                    f"{lim.min_edge_to_cost_ratio:.0f}x"
                )
        elif move_bps <= 0:
            rejections.append("NON_POSITIVE_TARGET")

    if stop is not None and stop >= price:
        rejections.append("INVALID_STOP")

    return TradabilityResult(
        symbol=symbol.upper(),
        passed=not rejections,
        rejections=rejections,
        notes=notes,
        avg_dollar_volume=adv,
        atr_pct=atr_pct,
        edge_to_cost_ratio=edge_ratio,
    )
