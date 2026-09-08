"""Current-price entry geometry for a confirmed, non-extended setup."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def current_entry_is_eligible(
    *,
    price: float,
    sma20: float | None,
    atr: float,
    thresholds: Any,
) -> bool:
    """Return whether current price is close enough to replace the SMA20 bid.

    This is not permission to buy. It only prevents a mechanically lower
    planned entry from forcing every otherwise valid setup into WAIT. Entry
    quality, admission, effective R:R and risk still decide execution.
    """
    if price <= 0 or atr <= 0 or sma20 is None or sma20 <= 0 or price < sma20:
        return False
    distance_fraction = (price - sma20) / sma20
    distance_atr = (price - sma20) / atr
    return distance_fraction <= float(thresholds.near_sma_frac) and distance_atr <= float(
        thresholds.atr_ext_max
    )


def current_entry_zone(
    *,
    price: float,
    atr: float,
    thresholds: Any,
) -> tuple[Decimal, Decimal]:
    """Build a narrow executable band ending at the evaluated current price."""
    width = max(
        float(thresholds.zone_min_width_atr) * atr,
        float(thresholds.zone_min_width_pct) * price,
    )
    low = Decimal(str(round(price - width, 4)))
    high = Decimal(str(round(price, 4)))
    return low, high
