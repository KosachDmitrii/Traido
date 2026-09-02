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


def unmet_wait_conditions(
    watch: object,
    facts: EntryTimingFacts,
    *,
    quote: Quote | None,
) -> list[str]:
    """Return required condition codes still failing after TRIGGERED."""
    th = get_entry_thresholds()
    missing: list[str] = []
    price = facts.current_price
    required = set(getattr(watch, "required_conditions", None) or [])

    if PRICE_ENTERS_ZONE in required and not price_in_zone(price, watch):  # type: ignore[arg-type]
        missing.append(PRICE_ENTERS_ZONE)

    if VWAP_HOLDS in required and (
        (
            facts.distance_from_vwap_pct is not None
            and facts.distance_from_vwap_pct < th.vwap_hold_min_pct
        )
        or (
            facts.anchor_price is not None and price < facts.anchor_price * th.vwap_anchor_hold_frac
        )
    ):
        missing.append(VWAP_HOLDS)

    if MOMENTUM_TURNS_POSITIVE in required:
        mom = facts.short_term_momentum_pct
        if mom is None or mom <= 0:
            missing.append(MOMENTUM_TURNS_POSITIVE)

    if PULLBACK_VOL_DIGESTING in required:
        ratio = facts.pullback_vol_ratio
        if ratio is not None and ratio > th.pullback_vol_digest_max:
            missing.append(PULLBACK_VOL_DIGESTING)

    max_spread = float(getattr(watch, "max_spread_bps", th.max_spread_bps))
    if SPREAD_ACCEPTABLE in required and quote is not None:
        bid = float(quote.bid)
        ask = float(quote.ask)
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10000
            if spread_bps > max_spread:
                missing.append(SPREAD_ACCEPTABLE)

    return missing
