"""Unified entry spread gate — desk preview, WAIT, admission, and approve.

One measurement rule (IEX buy-friction vs last print; SIP full book), one
ceiling from ``get_entry_thresholds()``, one feed resolver (IEX on paper, SIP
on live unless ``ALPACA_DATA_FEED`` overrides).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.config import Settings, get_settings
from core.schemas import Quote
from market_data.factory import resolve_alpaca_data_feed
from trading.entry_policy import EntryThresholds, get_entry_thresholds
from trading.gates import SPREAD_UNAVAILABLE, SpreadReading, SpreadSource, measure_spread


@dataclass(frozen=True)
class EntrySpreadGateResult:
    reading: SpreadReading
    bps: float | None
    max_bps: float
    feed: str
    reference_price: float | None
    acceptable: bool
    extreme: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            **self.reading.as_dict(),
            "max_spread_bps": self.max_bps,
            "feed": self.feed,
            "spread_reference_price": self.reference_price,
            "spread_acceptable": self.acceptable,
            "extreme_spread": self.extreme,
            "reason_codes": list(self.reason_codes),
        }


def resolve_spread_reference_price(
    quote: Quote,
    *,
    tape_last: float | None = None,
    facts_price: float | None = None,
    card_entry: float | None = None,
) -> float | None:
    """Canonical last for IEX buy-friction — tape first, then live facts, then mid."""
    if tape_last is not None and tape_last > 0:
        return float(tape_last)
    if facts_price is not None and facts_price > 0:
        return float(facts_price)
    bid = float(quote.bid or 0)
    ask = float(quote.ask or 0)
    if bid > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if card_entry is not None and card_entry > 0:
        return float(card_entry)
    return None


def evaluate_entry_spread(
    quote: Quote | None,
    *,
    now: datetime,
    tape_last: float | None = None,
    facts_price: float | None = None,
    card_entry: float | None = None,
    thresholds: EntryThresholds | None = None,
    feed: str | None = None,
    settings: Settings | None = None,
) -> EntrySpreadGateResult:
    """Measure spread and judge against the operator's aggressiveness ceiling."""
    th = thresholds or get_entry_thresholds()
    cfg = settings or get_settings()
    feed_key = feed or resolve_alpaca_data_feed(cfg)
    max_bps = th.max_spread_bps

    if quote is None:
        return EntrySpreadGateResult(
            reading=SPREAD_UNAVAILABLE,
            bps=None,
            max_bps=max_bps,
            feed=feed_key,
            reference_price=None,
            acceptable=False,
            extreme=False,
            reason_codes=("LIVE_QUOTE_REQUIRED",),
        )

    ref = resolve_spread_reference_price(
        quote,
        tape_last=tape_last,
        facts_price=facts_price,
        card_entry=card_entry,
    )
    reading = measure_spread(
        quote,
        now=now,
        max_age_sec=th.quote_max_age_sec,
        last_price=ref,
        feed=feed_key,
    )
    bps = reading.bps
    reasons: list[str] = []
    acceptable = False
    extreme = False

    if reading.source is SpreadSource.UNAVAILABLE:
        reasons.append("LIVE_QUOTE_REQUIRED")
    elif reading.source is SpreadSource.STALE:
        reasons.append("QUOTE_STALE")
    elif bps is not None:
        if bps > max_bps * 2:
            extreme = True
            reasons.append("EXTREME_SPREAD")
        if bps > max_bps:
            reasons.append("SPREAD_TOO_WIDE")
        else:
            acceptable = True

    return EntrySpreadGateResult(
        reading=reading,
        bps=bps,
        max_bps=max_bps,
        feed=feed_key,
        reference_price=ref,
        acceptable=acceptable,
        extreme=extreme,
        reason_codes=tuple(reasons),
    )
