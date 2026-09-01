"""
Volatility features.

Volatility drives position size, stop distance, and whether a setup is worth
taking at all. A 2R target is meaningless if 2R is inside the noise band.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from core.schemas import Bar
from quant.indicators import atr, bollinger, last, sma
from quant.series import closes, highs, lows

TRADING_DAYS = 252


@dataclass(frozen=True)
class VolatilityProfile:
    atr_value: float | None
    atr_pct: float | None
    """ATR as a percent of price — the natural unit for stop placement."""
    realised_vol_annual_pct: float | None
    vol_percentile: float | None
    """Current ATR% rank within its own trailing history, 0..1."""
    bollinger_bandwidth_pct: float | None
    squeeze: bool
    """Bandwidth in the bottom quintile — compression that often precedes expansion."""
    expansion: bool
    parkinson_vol_annual_pct: float | None
    """High-low range estimator. Less noisy than close-to-close."""
    reasons: list[str]

    def stop_distance_pct(self, atr_multiple: float = 1.5) -> float | None:
        """Suggested protective stop distance for this symbol's own noise level."""
        if self.atr_pct is None:
            return None
        return self.atr_pct * atr_multiple


def _percentile_rank(history: list[float], target: float) -> float | None:
    """Rank of `target` within `history`. Flat history ranks neutral, not extreme."""
    if len(history) < 10:
        return None
    if max(history) - min(history) <= 1e-9:
        return 0.5
    return sum(1 for v in history if v <= target) / len(history)


def realised_vol_annual_pct(values: list[float], lookback: int = 20) -> float | None:
    if len(values) < lookback + 2:
        return None
    window = values[-(lookback + 1) :]
    rets = [math.log(b / a) for a, b in pairwise(window) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0


def parkinson_vol_annual_pct(
    high: list[float], low: list[float], lookback: int = 20
) -> float | None:
    """Parkinson high-low volatility estimator, annualised."""
    if len(high) < lookback or len(low) < lookback:
        return None
    h, l = high[-lookback:], low[-lookback:]
    factor = 1.0 / (4.0 * math.log(2.0))
    total = 0.0
    n = 0
    for hi, lo in zip(h, l, strict=False):
        if hi <= 0 or lo <= 0 or hi < lo:
            continue
        total += math.log(hi / lo) ** 2
        n += 1
    if n < 2:
        return None
    return math.sqrt(factor * total / n) * math.sqrt(TRADING_DAYS) * 100.0


def compute_volatility(bars: list[Bar], *, history: int = 100) -> VolatilityProfile:
    c, h, l = closes(bars), highs(bars), lows(bars)
    price = c[-1] if c else 0.0
    reasons: list[str] = []

    atr_series = atr(h, l, c, 14)
    atr_now = last(atr_series)
    atr_pct = (atr_now / price * 100.0) if atr_now and price else None

    atr_history = [
        a / p * 100.0
        for a, p in zip(atr_series[-history:], c[-history:], strict=False)
        if a is not None and p > 0
    ]
    vol_rank = _percentile_rank(atr_history, atr_pct) if atr_pct is not None else None

    bb_u, bb_m, bb_l = bollinger(c, 20, 2.0)
    upper, mid, lower = last(bb_u), last(bb_m), last(bb_l)
    bandwidth = ((upper - lower) / mid * 100.0) if upper and lower and mid else None

    bandwidth_history: list[float] = []
    for u, m, lo in zip(bb_u[-history:], bb_m[-history:], bb_l[-history:], strict=False):
        if u is not None and m and lo is not None:
            bandwidth_history.append((u - lo) / m * 100.0)
    bw_rank = _percentile_rank(bandwidth_history, bandwidth) if bandwidth is not None else None

    squeeze = bw_rank is not None and bw_rank <= 0.20
    expansion = bw_rank is not None and bw_rank >= 0.80

    rv = realised_vol_annual_pct(c, 20)
    pv = parkinson_vol_annual_pct(h, l, 20)

    if atr_pct is not None:
        reasons.append(f"ATR {atr_pct:.2f}% of price")
    if vol_rank is not None:
        reasons.append(f"Volatility at {vol_rank * 100:.0f}th percentile of recent range")
    if squeeze:
        reasons.append("Bollinger squeeze — compressed range")
    if expansion:
        reasons.append("Range expanding — wider stops required")
    if rv is not None:
        reasons.append(f"Annualised realised volatility {rv:.1f}%")

    return VolatilityProfile(
        atr_value=atr_now,
        atr_pct=atr_pct,
        realised_vol_annual_pct=rv,
        vol_percentile=vol_rank,
        bollinger_bandwidth_pct=bandwidth,
        squeeze=squeeze,
        expansion=expansion,
        parkinson_vol_annual_pct=pv,
        reasons=reasons,
    )


def average_dollar_volume(bars: list[Bar], lookback: int = 20) -> float | None:
    """Median-ish liquidity proxy: mean close * volume over the lookback."""
    if len(bars) < lookback:
        return None
    window = bars[-lookback:]
    total = sum(float(b.close) * float(b.volume) for b in window)
    return total / len(window)


def volume_trend(bars: list[Bar], short: int = 5, long: int = 20) -> float | None:
    """Short-window average volume divided by long-window. Above 1 means participation rising."""
    vols = [float(b.volume) for b in bars]
    s, l = last(sma(vols, short)), last(sma(vols, long))
    if not s or not l:
        return None
    return s / l
