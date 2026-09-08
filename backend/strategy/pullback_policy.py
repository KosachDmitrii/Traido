"""Canonical coarse price-location bands for the active pullback strategy."""

from __future__ import annotations

PULLBACK_NEAR_SMA_FRACTION = 0.025
PULLBACK_MAX_ABOVE_SMA_FRACTION = 0.04


def pullback_readiness(distance_from_sma: float) -> tuple[int, float]:
    """Return a deterministic priority tier and continuous readiness score.

    Tier zero matches the strategy's preferred SMA20 neighbourhood. Tier one
    remains admissible but less timely. Tier two is materially displaced and
    may still fill spare analysis capacity, but cannot outrank a ready pullback
    merely because momentum is stronger.
    """
    distance = abs(distance_from_sma)
    if distance <= PULLBACK_NEAR_SMA_FRACTION:
        tier = 0
    elif distance <= PULLBACK_MAX_ABOVE_SMA_FRACTION:
        tier = 1
    else:
        tier = 2
    score = max(0.0, 1.0 - distance / PULLBACK_MAX_ABOVE_SMA_FRACTION)
    return tier, score
