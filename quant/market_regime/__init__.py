"""
Market regime detection.

Two jobs:

1. Live — label the current tape so the Strategy Agent can refuse setups that
   only work in conditions that are not present today.
2. Evaluation — split a long history into regime segments so a backtest can be
   judged in bull, bear, sideways, high-vol and low-vol separately instead of
   hiding a losing regime inside one flattering aggregate number.

Deterministic. No LLM, no lookahead: every label at index i uses bars[:i+1].
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from core.enums import MarketRegimeLabel
from core.schemas import Bar
from quant.indicators import atr, ema, last, sma
from quant.series import closes, highs, lows

TREND_LOOKBACK = 50
SLOW_LOOKBACK = 200
VOL_LOOKBACK = 100

HIGH_VOL_PERCENTILE = 0.80
LOW_VOL_PERCENTILE = 0.20
TREND_FLAT_PCT = 3.0
"""Below this absolute move over the trend window the tape counts as sideways."""


@dataclass(frozen=True)
class RegimeSnapshot:
    label: MarketRegimeLabel
    trend_strength_pct: float
    """Signed % distance of price from its slow average — direction and conviction."""
    volatility_pct: float
    """ATR as a percent of price."""
    volatility_percentile: float | None
    """Where current volatility sits in its own recent history, 0..1."""
    breadth_note: str
    reasons: list[str]

    @property
    def is_risk_on(self) -> bool:
        return self.label in (MarketRegimeLabel.BULLISH, MarketRegimeLabel.RISK_ON)

    @property
    def is_tradable_long(self) -> bool:
        """Long-only V1: refuse bearish and violently unstable tape."""
        return self.label not in (
            MarketRegimeLabel.BEARISH,
            MarketRegimeLabel.RISK_OFF,
            MarketRegimeLabel.HIGH_VOLATILITY,
        )


def _percentile_rank(values: list[float], target: float) -> float | None:
    """
    Where `target` sits within `values`, 0..1.

    A history with no dispersion has no percentiles to speak of, so it ranks
    neutral. Without this guard a perfectly steady volatility reads as the
    100th percentile and every symbol looks like a volatility blow-off.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < 5:
        return None
    spread = max(clean) - min(clean)
    if spread <= 1e-9:
        return 0.5
    below = sum(1 for v in clean if v <= target)
    return below / len(clean)


def classify(bars: list[Bar]) -> RegimeSnapshot:
    """Label the regime as of the last bar in `bars`."""
    c = closes(bars)
    if len(c) < 20:
        return RegimeSnapshot(
            label=MarketRegimeLabel.NEUTRAL,
            trend_strength_pct=0.0,
            volatility_pct=0.0,
            volatility_percentile=None,
            breadth_note="insufficient history",
            reasons=["Not enough bars to classify regime"],
        )

    price = c[-1]
    reasons: list[str] = []

    fast = last(ema(c, min(TREND_LOOKBACK, len(c) - 1)))
    slow = last(sma(c, min(SLOW_LOOKBACK, len(c) - 1)))
    anchor = slow if slow is not None else fast
    trend_pct = ((price - anchor) / anchor * 100.0) if anchor else 0.0

    atr_series = atr(highs(bars), lows(bars), c, 14)
    atr_now = last(atr_series)
    vol_pct = (atr_now / price * 100.0) if atr_now and price else 0.0

    history = [
        (a / p * 100.0)
        for a, p in zip(atr_series[-VOL_LOOKBACK:], c[-VOL_LOOKBACK:], strict=False)
        if a is not None and p
    ]
    vol_rank = _percentile_rank(history, vol_pct)

    label = MarketRegimeLabel.NEUTRAL

    if vol_rank is not None and vol_rank >= HIGH_VOL_PERCENTILE:
        label = MarketRegimeLabel.HIGH_VOLATILITY
        reasons.append(f"Volatility in top {(1 - vol_rank) * 100:.0f}% of recent range")
    elif trend_pct >= TREND_FLAT_PCT and (fast is None or slow is None or fast >= slow):
        label = MarketRegimeLabel.BULLISH
        reasons.append(f"Price {trend_pct:.1f}% above long average")
    elif trend_pct <= -TREND_FLAT_PCT and (fast is None or slow is None or fast <= slow):
        label = MarketRegimeLabel.BEARISH
        reasons.append(f"Price {trend_pct:.1f}% below long average")
    else:
        reasons.append(f"Price within {TREND_FLAT_PCT:.0f}% of long average — range")

    if vol_rank is not None and vol_rank <= LOW_VOL_PERCENTILE:
        reasons.append("Volatility compressed vs recent history")

    reasons.append(f"ATR {vol_pct:.2f}% of price")

    return RegimeSnapshot(
        label=label,
        trend_strength_pct=trend_pct,
        volatility_pct=vol_pct,
        volatility_percentile=vol_rank,
        breadth_note="single-symbol proxy",
        reasons=reasons,
    )


@dataclass(frozen=True)
class RegimeSegment:
    label: MarketRegimeLabel
    start_index: int
    end_index: int
    start_ts: datetime
    end_ts: datetime
    bars: list[Bar]

    @property
    def length(self) -> int:
        return len(self.bars)


def segment_by_regime(
    bars: list[Bar],
    *,
    window: int = 60,
    min_segment: int = 40,
) -> list[RegimeSegment]:
    """
    Split history into contiguous same-regime runs.

    Used by the evaluation harness so a strategy is scored in each regime
    separately. Short runs are merged into the previous segment to avoid
    fragmenting the series into statistically useless slivers.
    """
    if len(bars) < window + min_segment:
        return []

    labels: list[MarketRegimeLabel] = []
    for i in range(window, len(bars)):
        labels.append(classify(bars[: i + 1]).label)

    segments: list[RegimeSegment] = []
    run_label = labels[0]
    run_start = window

    def flush(label: MarketRegimeLabel, start: int, end: int) -> None:
        chunk = bars[start:end]
        if not chunk:
            return
        if segments and len(chunk) < min_segment:
            prev = segments[-1]
            merged = bars[prev.start_index : end]
            segments[-1] = RegimeSegment(
                label=prev.label,
                start_index=prev.start_index,
                end_index=end,
                start_ts=prev.start_ts,
                end_ts=chunk[-1].ts,
                bars=merged,
            )
            return
        segments.append(
            RegimeSegment(
                label=label,
                start_index=start,
                end_index=end,
                start_ts=chunk[0].ts,
                end_ts=chunk[-1].ts,
                bars=chunk,
            )
        )

    for offset, label in enumerate(labels[1:], start=1):
        if label != run_label:
            flush(run_label, run_start, window + offset)
            run_label = label
            run_start = window + offset

    flush(run_label, run_start, len(bars))
    return [s for s in segments if s.length >= min_segment]


def annualised_volatility_pct(bars: list[Bar], periods_per_year: float = 252.0) -> float | None:
    """Standard deviation of log returns, annualised. Used by sizing and filters."""
    c = closes(bars)
    if len(c) < 20:
        return None
    rets = [math.log(b / a) for a, b in pairwise(c) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year) * 100.0
