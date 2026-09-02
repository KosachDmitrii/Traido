"""Impulse leg + pullback metrics — desk-style continuation entry geometry.

Professional pullback trading measures:
- the up-leg (impulse): size in ATR, bar count, volume participation
- the down-leg (pullback): fib retracement, bar count, volume vs impulse
- how many prior retests of the anchor occurred (pullback index)

Computed from OHLCV only; no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.schemas import Bar
from quant.series import closes, highs, lows, volumes


@dataclass(frozen=True)
class ImpulsePullbackMetrics:
    impulse_low: float | None
    impulse_high: float | None
    impulse_range: float | None
    impulse_range_atr: float | None
    impulse_bars: int | None
    retracement_pct: float | None
    pullback_bars: int | None
    impulse_vol_avg: float | None
    pullback_vol_avg: float | None
    pullback_vol_ratio: float | None
    impulse_grade: str
    pullback_index: int


def _grade_impulse(
    *,
    range_atr: float | None,
    impulse_bars: int,
    directional_frac: float,
) -> str:
    if range_atr is None or range_atr < 0.6:
        return "C"
    if range_atr > 4.0:
        return "C"
    if range_atr >= 1.0 and impulse_bars >= 3 and directional_frac >= 0.55:
        return "A"
    if range_atr >= 0.6 and impulse_bars >= 2:
        return "B"
    return "C"


def _pullback_index(closes_: list[float], anchor: float) -> int:
    """Count distinct dips through anchor after first touch above it."""
    if not closes_ or anchor <= 0:
        return 0
    count = 0
    was_above = False
    for c in closes_:
        if c >= anchor:
            was_above = True
        elif was_above and c < anchor:
            count += 1
            was_above = False
    return count


def compute_impulse_pullback(
    bars: list[Bar],
    atr: float | None,
    *,
    anchor: float | None = None,
    lookback: int = 40,
    impulse_window: int = 20,
) -> ImpulsePullbackMetrics:
    empty = ImpulsePullbackMetrics(
        impulse_low=None,
        impulse_high=None,
        impulse_range=None,
        impulse_range_atr=None,
        impulse_bars=None,
        retracement_pct=None,
        pullback_bars=None,
        impulse_vol_avg=None,
        pullback_vol_avg=None,
        pullback_vol_ratio=None,
        impulse_grade="C",
        pullback_index=0,
    )
    if len(bars) < 8:
        return empty

    segment = bars[-lookback:]
    c = closes(segment)
    h = highs(segment)
    l = lows(segment)
    v = volumes(segment)
    if not c:
        return empty

    win = min(impulse_window, len(segment))
    hi_rel = max(range(win), key=lambda i: h[-win + i])
    hi_abs = len(segment) - win + hi_rel
    impulse_high = h[hi_abs]

    low_rel = min(range(hi_abs + 1), key=lambda i: l[i])
    impulse_low = l[low_rel]
    impulse_range = impulse_high - impulse_low
    if impulse_range <= 0:
        return empty

    price = c[-1]
    retracement = None
    if price < impulse_high:
        retracement = (impulse_high - price) / impulse_range
    elif price >= impulse_high:
        retracement = 0.0

    impulse_bars = max(1, hi_abs - low_rel + 1)
    pullback_bars = max(0, len(segment) - 1 - hi_abs)

    impulse_vols = v[low_rel : hi_abs + 1]
    pullback_vols = v[hi_abs + 1 :] if hi_abs + 1 < len(v) else []
    impulse_vol_avg = sum(impulse_vols) / len(impulse_vols) if impulse_vols else None
    pullback_vol_avg = sum(pullback_vols) / len(pullback_vols) if pullback_vols else None
    vol_ratio = None
    if impulse_vol_avg and impulse_vol_avg > 0 and pullback_vol_avg is not None:
        vol_ratio = pullback_vol_avg / impulse_vol_avg

    range_atr = impulse_range / atr if atr and atr > 0 else None
    up_bars = sum(
        1
        for i in range(low_rel, hi_abs + 1)
        if c[i] > float(segment[i].open)
    )
    bar_count = hi_abs - low_rel + 1
    directional_frac = up_bars / bar_count if bar_count > 0 else 0.0

    anchor_px = anchor if anchor and anchor > 0 else (impulse_low + impulse_high) / 2.0
    pb_index = _pullback_index(c[low_rel:], anchor_px)

    grade = _grade_impulse(
        range_atr=range_atr,
        impulse_bars=impulse_bars,
        directional_frac=directional_frac,
    )

    return ImpulsePullbackMetrics(
        impulse_low=round(impulse_low, 6),
        impulse_high=round(impulse_high, 6),
        impulse_range=round(impulse_range, 6),
        impulse_range_atr=round(range_atr, 4) if range_atr is not None else None,
        impulse_bars=impulse_bars,
        retracement_pct=round(retracement, 4) if retracement is not None else None,
        pullback_bars=pullback_bars,
        impulse_vol_avg=round(impulse_vol_avg, 2) if impulse_vol_avg else None,
        pullback_vol_avg=round(pullback_vol_avg, 2) if pullback_vol_avg else None,
        pullback_vol_ratio=round(vol_ratio, 4) if vol_ratio is not None else None,
        impulse_grade=grade,
        pullback_index=pb_index,
    )


def metrics_to_indicators(m: ImpulsePullbackMetrics) -> dict[str, float | int | str | None]:
    return {
        "impulse_low": m.impulse_low,
        "impulse_high": m.impulse_high,
        "impulse_range": m.impulse_range,
        "impulse_range_atr": m.impulse_range_atr,
        "impulse_bars": m.impulse_bars,
        "retracement_pct": m.retracement_pct,
        "pullback_bars": m.pullback_bars,
        "impulse_vol_avg": m.impulse_vol_avg,
        "pullback_vol_avg": m.pullback_vol_avg,
        "pullback_vol_ratio": m.pullback_vol_ratio,
        "impulse_grade": m.impulse_grade,
        "pullback_index": m.pullback_index,
    }
