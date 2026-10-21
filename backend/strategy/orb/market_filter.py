"""Deterministic intraday market filter for calibrated ORB entries.

A long breakout in a falling tape is a fight with the index. The filter reads
only completed M5 bars: the benchmark must hold above its session VWAP, must
not be down hard on the day, and the candidate must be leading it from the
open while above its own VWAP. Missing or stale bars are a refusal, never a
pass — the same rule every other gate on this path follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from core.clock import ET
from core.enums import Timeframe
from core.schemas import Bar

DATA_MISSING = "MARKET_FILTER_DATA_MISSING"
BELOW_VWAP = "MARKET_BELOW_VWAP"
DOWN_DAY = "MARKET_DOWN_DAY"
WEAK_RELATIVE_STRENGTH = "RELATIVE_STRENGTH_WEAK"
SYMBOL_BELOW_VWAP = "SYMBOL_BELOW_VWAP"


@dataclass(frozen=True)
class MarketFilterResult:
    passed: bool
    data_ok: bool
    reasons: tuple[str, ...]
    measured: dict[str, Any] = field(default_factory=dict)


def _session_bars(bars: list[Bar], now: datetime) -> list[Bar] | None:
    """Completed, contiguous M5 bars from 09:30 ET, ending at the latest window."""
    local = now.astimezone(ET)
    opening = datetime.combine(local.date(), time(9, 30), ET)
    latest = local.replace(minute=local.minute - local.minute % 5, second=0, microsecond=0)
    rows = sorted(
        (b for b in bars if b.ts >= opening and b.ts + timedelta(minutes=5) <= now),
        key=lambda b: b.ts,
    )
    expected = opening
    for b in rows:
        if b.ts != expected or b.volume < 0 or b.low <= 0:
            return None
        expected = b.ts + timedelta(minutes=5)
    # One window of vendor lag is tolerated; more is a stale tape.
    if not rows or expected < latest - timedelta(minutes=5):
        return None
    return rows


def _vwaps(rows: list[Bar]) -> list[Decimal] | None:
    total_pv, total_v, out = Decimal(0), Decimal(0), []
    for b in rows:
        total_pv += (b.high + b.low + b.close) / 3 * b.volume
        total_v += b.volume
        if total_v <= 0:
            return None
        out.append(total_pv / total_v)
    return out


def evaluate_intraday_market(
    *,
    benchmark_bars: list[Bar],
    benchmark_prior_close: Decimal | None,
    symbol_bars: list[Bar],
    now: datetime,
    parameters: dict[str, Any],
) -> MarketFilterResult:
    spy = _session_bars(benchmark_bars, now)
    stock = _session_bars(symbol_bars, now)
    spy_vwap = _vwaps(spy) if spy else None
    stock_vwap = _vwaps(stock) if stock else None
    closes = int(parameters["market_vwap_closes"])
    if (
        spy is None
        or stock is None
        or spy_vwap is None
        or stock_vwap is None
        or len(spy) < closes
        or benchmark_prior_close is None
        or not benchmark_prior_close.is_finite()
        or benchmark_prior_close <= 0
    ):
        return MarketFilterResult(False, False, (DATA_MISSING,))
    spy_return = spy[-1].close / spy[0].open - 1
    stock_return = stock[-1].close / stock[0].open - 1
    day_change_pct = (spy[-1].close / benchmark_prior_close - 1) * 100
    measured = {
        "benchmark_close": str(spy[-1].close),
        "benchmark_vwap": str(spy_vwap[-1]),
        "benchmark_day_change_pct": str(day_change_pct),
        "benchmark_return_from_open": str(spy_return),
        "symbol_return_from_open": str(stock_return),
        "symbol_close": str(stock[-1].close),
        "symbol_vwap": str(stock_vwap[-1]),
        "last_bar": spy[-1].ts.isoformat(),
    }
    reasons = []
    # Two closes above VWAP, so a single print across the line does not flip it.
    if any(b.close <= v for b, v in zip(spy[-closes:], spy_vwap[-closes:], strict=True)):
        reasons.append(BELOW_VWAP)
    if day_change_pct <= -Decimal(parameters["market_max_drop_pct"]):
        reasons.append(DOWN_DAY)
    if stock_return < spy_return:
        reasons.append(WEAK_RELATIVE_STRENGTH)
    if stock[-1].close <= stock_vwap[-1]:
        reasons.append(SYMBOL_BELOW_VWAP)
    return MarketFilterResult(not reasons, True, tuple(reasons), measured)


async def assess_intraday_market(
    symbol: str, market_data: Any, *, now: datetime, parameters: dict[str, Any]
) -> MarketFilterResult:
    benchmark = parameters["market_benchmark"]
    local = now.astimezone(ET)
    opening = datetime.combine(local.date(), time(9, 30), ET)
    try:
        spy, stock, daily = (
            await market_data.get_bars(benchmark, Timeframe.M5, opening, now),
            await market_data.get_bars(symbol, Timeframe.M5, opening, now),
            await market_data.get_bars(
                benchmark, Timeframe.D1, opening - timedelta(days=10), opening
            ),
        )
    except Exception:  # noqa: BLE001 — an unread tape is a refusal, not a pass
        return MarketFilterResult(False, False, (DATA_MISSING,))
    from strategy.orb import _previous_sessions

    # The previous session's own bar, never an older one from a stalled feed.
    session = _previous_sessions(now, 1)[0]
    prior = [b for b in daily if b.ts.astimezone(ET).date() == session]
    prior_close = prior[-1].close if len(prior) == 1 else None
    return evaluate_intraday_market(
        benchmark_bars=spy,
        benchmark_prior_close=prior_close,
        symbol_bars=stock,
        now=now,
        parameters=parameters,
    )
