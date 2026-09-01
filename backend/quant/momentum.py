"""
Momentum features.

Momentum is the most robust documented cross-sectional equity anomaly, but the
naive version buys the top of a blow-off. Two guards are built in here:

- Risk-adjusted momentum divides the move by its own volatility, so a smooth
  30% advance ranks above a violent 30% one.
- 12-1 momentum skips the most recent period, because short-horizon returns
  mean-revert and contaminate the signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from core.schemas import Bar
from quant.series import closes

DEFAULT_LOOKBACKS = (21, 63, 126, 252)


@dataclass(frozen=True)
class MomentumProfile:
    roc: dict[int, float]
    """Rate of change in percent, keyed by lookback in bars."""
    risk_adjusted: float | None
    """Medium-horizon return divided by realised volatility over the same window."""
    momentum_12_1: float | None
    """252-bar return excluding the most recent 21 bars."""
    accelerating: bool
    """Short-horizon momentum exceeds long-horizon — the move is speeding up."""
    consistency: float | None
    """Share of positive periods over the medium window, 0..1."""
    reasons: list[str]

    def score(self) -> int:
        """0-100 momentum score, blended and clamped. Neutral is 50."""
        parts: list[float] = []
        if self.momentum_12_1 is not None:
            parts.append(_clamp(50 + self.momentum_12_1 * 1.2, 0, 100))
        medium = self.roc.get(63)
        if medium is not None:
            parts.append(_clamp(50 + medium * 2.0, 0, 100))
        if self.risk_adjusted is not None:
            parts.append(_clamp(50 + self.risk_adjusted * 25, 0, 100))
        if self.consistency is not None:
            parts.append(self.consistency * 100)
        if not parts:
            return 50
        base = sum(parts) / len(parts)
        if self.accelerating:
            base += 5
        return round(_clamp(base, 0, 100))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def rate_of_change(values: list[float], lookback: int) -> float | None:
    if len(values) <= lookback or lookback <= 0:
        return None
    past = values[-(lookback + 1)]
    if past <= 0:
        return None
    return (values[-1] - past) / past * 100.0


def realised_volatility(values: list[float], lookback: int) -> float | None:
    """Stdev of log returns over `lookback` bars, in percent (not annualised)."""
    if len(values) < lookback + 2:
        return None
    window = values[-(lookback + 1) :]
    rets = [math.log(b / a) for a, b in pairwise(window) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 100.0


def compute_momentum(
    bars: list[Bar],
    lookbacks: tuple[int, ...] = DEFAULT_LOOKBACKS,
) -> MomentumProfile:
    c = closes(bars)
    roc = {lb: r for lb in lookbacks if (r := rate_of_change(c, lb)) is not None}
    reasons: list[str] = []

    medium = roc.get(63)
    vol = realised_volatility(c, 63)
    risk_adj = None
    if medium is not None and vol and vol > 0:
        risk_adj = medium / (vol * math.sqrt(63))

    mom_12_1 = None
    if len(c) > 252:
        anchor, recent = c[-253], c[-22]
        if anchor > 0:
            mom_12_1 = (recent - anchor) / anchor * 100.0

    short, long = roc.get(21), roc.get(126)
    accelerating = short is not None and long is not None and short * 6 > long

    consistency = None
    if len(c) > 63:
        window = c[-64:]
        ups = sum(1 for a, b in pairwise(window) if b > a)
        consistency = ups / (len(window) - 1)

    if mom_12_1 is not None:
        reasons.append(f"12-1 momentum {mom_12_1:+.1f}%")
    if medium is not None:
        reasons.append(f"3-month return {medium:+.1f}%")
    if risk_adj is not None:
        reasons.append(f"Risk-adjusted momentum {risk_adj:+.2f}")
    if accelerating:
        reasons.append("Short-horizon momentum accelerating")

    return MomentumProfile(
        roc=roc,
        risk_adjusted=risk_adj,
        momentum_12_1=mom_12_1,
        accelerating=accelerating,
        consistency=consistency,
        reasons=reasons,
    )
