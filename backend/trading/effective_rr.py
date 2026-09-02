"""Effective risk/reward after spread and expected slippage."""

from __future__ import annotations

from decimal import Decimal

from core.schemas import EffectiveRRResult, Quote

DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_ADMISSION_RR_FLOOR = 2.0
EXCEPTIONAL_RR_FLOOR = 1.8
WEAK_SETUP_RR_FLOOR = 2.5


def compute_effective_rr(
    *,
    entry: Decimal | float,
    stop: Decimal | float,
    target: Decimal | float,
    quote: Quote | None = None,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
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
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10000
            effective_entry = ask * (1.0 + slippage_bps / 10000.0)
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
) -> float:
    """Formal exceptional path — not 'company looks strong'."""
    exceptional = (
        setup_quality >= 85
        and entry_quality >= 80
        and chase_score < 30
        and structure_valid
        and not warnings
    )
    if exceptional:
        return EXCEPTIONAL_RR_FLOOR
    if setup_quality < 55:
        return WEAK_SETUP_RR_FLOOR
    return DEFAULT_ADMISSION_RR_FLOOR
