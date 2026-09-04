"""Check WAIT trigger conditions before a watch may become BUY_NOW."""

from __future__ import annotations

from core.schemas import EntryTimingFacts, Quote
from trading.entry_policy import get_entry_thresholds
from trading.entry_watches import (
    MOMENTUM_TURNS_POSITIVE,
    PRICE_ENTERS_ZONE,
    PULLBACK_VOL_DIGESTING,
    SPREAD_ACCEPTABLE,
    VWAP_HOLDS,
    price_in_zone,
)

# Quote-only checks — failing them must not demote TRIGGERED → WAITING or the
# card flaps every tick while price sits in the cushion band.
TRANSIENT_TRIGGER_CONDITIONS = frozenset({SPREAD_ACCEPTABLE})


def unmet_wait_conditions(
    watch: object,
    facts: EntryTimingFacts,
    *,
    quote: Quote | None,
) -> list[str]:
    """Return required condition codes still failing after TRIGGERED."""
    th = get_entry_thresholds()
    price = facts.current_price
    required = set(getattr(watch, "required_conditions", None) or [])
    missing: list[str] = []

    if PRICE_ENTERS_ZONE in required and not price_in_zone(price, watch):  # type: ignore[arg-type]
        missing.append(PRICE_ENTERS_ZONE)

    if VWAP_HOLDS in required and th.require_vwap_hold and (
        (
            facts.distance_from_vwap_pct is not None
            and facts.distance_from_vwap_pct < th.vwap_hold_min_pct
        )
        or (
            facts.anchor_price is not None and price < facts.anchor_price * th.vwap_anchor_hold_frac
        )
    ):
        missing.append(VWAP_HOLDS)

    if MOMENTUM_TURNS_POSITIVE in required and th.require_momentum_flip:
        mom = facts.short_term_momentum_pct
        if mom is None or mom <= th.momentum_min_pct:
            missing.append(MOMENTUM_TURNS_POSITIVE)

    if PULLBACK_VOL_DIGESTING in required and th.require_vol_digest:
        ratio = facts.pullback_vol_ratio
        if ratio is not None and ratio > th.pullback_vol_digest_max:
            missing.append(PULLBACK_VOL_DIGESTING)

    if SPREAD_ACCEPTABLE in required and quote is not None:
        from trading import execution as execution_mod
        from trading.entry_spread_gate import evaluate_entry_spread

        spread_gate = evaluate_entry_spread(
            quote,
            now=execution_mod._utcnow(),
            facts_price=float(price) if price is not None else None,
            thresholds=th,
        )
        if not spread_gate.acceptable and spread_gate.reason_codes:
            missing.append(SPREAD_ACCEPTABLE)

    return missing
