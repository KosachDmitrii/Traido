"""Effective risk/reward after spread and expected slippage."""

from __future__ import annotations

import math
from decimal import Decimal

from core.schemas import EffectiveRRResult, Quote

DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_ADMISSION_RR_FLOOR = 2.0
EXCEPTIONAL_RR_FLOOR = 1.8
WEAK_SETUP_RR_FLOOR = 2.5


def planned_long_rr(
    entry: Decimal | float,
    stop: Decimal | float,
    target: Decimal | float,
) -> float | None:
    """Canonical long R:R from the same entry/stop/target admission sizes against.

    risk   = entry - stop
    reward = target - entry
    RR     = reward / risk

    Returns None when geometry is invalid or RR is non-finite.
    """
    try:
        entry_f = float(entry)
        stop_f = float(stop)
        target_f = float(target)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry_f) and math.isfinite(stop_f) and math.isfinite(target_f)):
        return None
    if stop_f >= entry_f or target_f <= entry_f:
        return None
    risk = entry_f - stop_f
    reward = target_f - entry_f
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if not math.isfinite(rr):
        return None
    return rr


def price_within_zone_cushion(
    *,
    price: float,
    zone_low: float | None,
    zone_high: float | None,
    atr: float | None,
    cushion_atr: float = 0.20,
) -> bool:
    """True when price is inside the printed zone ± cushion_atr × ATR."""
    if zone_low is None or zone_high is None or atr is None or atr <= 0:
        return False
    buf = atr * cushion_atr
    return zone_low - buf <= price <= zone_high + buf


def compute_effective_rr(
    *,
    entry: Decimal | float,
    stop: Decimal | float,
    target: Decimal | float,
    quote: Quote | None = None,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    zone_low: float | None = None,
    zone_high: float | None = None,
    atr: float | None = None,
    cushion_atr: float = 0.20,
) -> EffectiveRRResult:
    """Effective entry uses ask + slippage; reward uses target minus entry-side cost."""
    entry_f = float(entry)
    stop_f = float(stop)
    target_f = float(target)

    spread_bps: float | None = None
    if quote is not None:
        bid = float(quote.bid or 0)
        ask = float(quote.ask or entry_f)
        if bid > 0 and ask >= bid:
            from core.config import get_settings
            from market_data.entry_spread import spread_bps_for_entry
            from market_data.factory import resolve_alpaca_data_feed

            spread_bps = spread_bps_for_entry(
                quote,
                last_price=entry_f,
                feed=resolve_alpaca_data_feed(get_settings()),
            )
            if ask > stop_f:
                effective_entry = ask * (1.0 + slippage_bps / 10000.0)
            else:
                effective_entry = entry_f * (1.0 + slippage_bps / 10000.0)
        else:
            effective_entry = entry_f * (1.0 + slippage_bps / 10000.0)
    else:
        effective_entry = entry_f * (1.0 + slippage_bps / 10000.0)

    effective_stop = stop_f
    effective_target = target_f
    effective_risk = max(effective_entry - effective_stop, 1e-9)
    effective_reward = max(effective_target - effective_entry, 0.0)
    effective_rr = effective_reward / effective_risk if effective_risk > 0 else 0.0

    return EffectiveRRResult(
        effective_entry=round(effective_entry, 4),
        effective_stop=round(effective_stop, 4),
        effective_target=round(effective_target, 4),
        effective_risk=round(effective_risk, 4),
        effective_reward=round(effective_reward, 4),
        effective_rr=round(effective_rr, 4),
        spread_bps=round(spread_bps, 2) if spread_bps is not None else None,
        slippage_bps=slippage_bps,
    )


def required_admission_rr(
    *,
    setup_quality: int,
    entry_quality: int,
    chase_score: int,
    structure_valid: bool,
    warnings: list[str],
    min_rr_floor: float | None = None,
    weak_setup_rr_floor: float | None = None,
) -> float:
    """Formal exceptional path — not 'company looks strong'."""
    floor = min_rr_floor if min_rr_floor is not None else DEFAULT_ADMISSION_RR_FLOOR
    weak_floor = weak_setup_rr_floor if weak_setup_rr_floor is not None else WEAK_SETUP_RR_FLOOR
    exceptional = (
        setup_quality >= 85
        and entry_quality >= 80
        and chase_score < 30
        and structure_valid
        and not warnings
    )
    if exceptional:
        return min(EXCEPTIONAL_RR_FLOOR, floor)
    if setup_quality < 55:
        return max(weak_floor, floor)
    return floor
