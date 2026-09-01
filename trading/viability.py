"""Live viability of a standing BUY card — without withdrawing it.

A proposal is a photograph of a setup, and the hour-long TTL exists so a human
can confirm it. Between the photograph and the click the book can move: the
spread can open, the offer can climb past a quarter of the planned risk, or the
price can walk through the stop or the target. Those are transient. They come
back. Withdrawing the card on them would delete a good setup because it was
briefly unbuyable — which is why `withdraw_unactionable` deliberately ignores
them.

What they *must* not do is leave the BUY button looking live. The operator then
presses it to discover, one toast later, that the click was never going to
place. That is not fail-closed; it is fail-after-click.

This module answers the desk's question with the same geometry the entry path
uses at decide time: same buffer, same spread ceiling, same 0.25R allowance.
The decide path still re-checks — this is a preview, not a waiver. A card that
reads `live` can still refuse on bars, ADV, RTH or reconciliation; a card that
reads anything else must not invite the press.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.schemas import Quote, TradeCandidate
from trading.gates import LiquidityPolicy, SpreadReading, SpreadSource, measure_spread
from trading.pricing import ENTRY_BUFFER_BPS, marketable_buy_limit

# Keep the decide-time constant in one place. Importing from execution would
# pull the whole service graph into a desk poll; the number is the contract.
MAX_ENTRY_SLIPPAGE_R = 0.25

LIVE = "live"
WIDE = "wide"
DRIFTED = "drifted"
PAST_SETUP = "past_setup"
UNVERIFIED = "unverified"

_STATE_FOR_REASON = {
    "SPREAD_TOO_WIDE": WIDE,
    "ENTRY_TOO_FAR_ABOVE_CARD": DRIFTED,
    "PRICE_MOVED_PAST_SETUP": PAST_SETUP,
    "QUOTE_STALE": UNVERIFIED,
    "LIVE_QUOTE_REQUIRED": UNVERIFIED,
}


@dataclass(frozen=True)
class BuyViability:
    """Whether pressing BUY on this card is currently worth attempting."""

    state: str
    buyable: bool
    reasons: tuple[str, ...]
    measured: dict[str, Any]
    as_of: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "buyable": self.buyable,
            "reasons": list(self.reasons),
            "measured": self.measured,
            "as_of": self.as_of.isoformat(),
        }


def assess_buy_viability(
    candidate: TradeCandidate,
    quote: Quote | None,
    *,
    spread: SpreadReading | None = None,
    now: datetime | None = None,
    max_spread_bps: float | None = None,
    entry_buffer_bps: float = ENTRY_BUFFER_BPS,
    max_entry_slippage_r: float = MAX_ENTRY_SLIPPAGE_R,
    max_quote_age_sec: float | None = None,
) -> BuyViability:
    """The same price/spread geometry decide will enforce — as a desk preview.

    Deliberately does not run ADV, participation or bar history. Those are
    order-shape checks that need sized qty and a bar window; they still refuse
    at decide time. What the operator needs before the click is whether the
    *card's levels* still describe a trade against the live book.

    `spread` may be supplied by a caller that already read the same quote —
    decide does, so the desk preview and the click judge one snapshot rather
    than two.
    """
    as_of = now or datetime.now(UTC)
    policy = LiquidityPolicy()
    spread_cap = policy.max_spread_bps if max_spread_bps is None else max_spread_bps
    age_cap = policy.max_quote_age_sec if max_quote_age_sec is None else max_quote_age_sec
    reading = (
        spread if spread is not None else measure_spread(quote, now=as_of, max_age_sec=age_cap)
    )

    measured: dict[str, Any] = {
        "symbol": candidate.symbol,
        "card_entry": str(candidate.entry),
        **reading.as_dict(),
    }

    if quote is None or not reading.is_live:
        reason = "QUOTE_STALE" if reading.source is SpreadSource.STALE else "LIVE_QUOTE_REQUIRED"
        return BuyViability(
            state=UNVERIFIED,
            buyable=False,
            reasons=(reason,),
            measured=measured,
            as_of=as_of,
        )

    if reading.bps is not None and reading.bps > spread_cap:
        return BuyViability(
            state=WIDE,
            buyable=False,
            reasons=("SPREAD_TOO_WIDE",),
            measured={**measured, "max_spread_bps": spread_cap},
            as_of=as_of,
        )

    limit = marketable_buy_limit(quote.ask, buffer_bps=entry_buffer_bps)
    measured["ask"] = str(quote.ask)
    measured["limit_price"] = str(limit)

    if limit <= candidate.stop or limit >= candidate.target:
        return BuyViability(
            state=PAST_SETUP,
            buyable=False,
            reasons=("PRICE_MOVED_PAST_SETUP",),
            measured=measured,
            as_of=as_of,
        )

    risk_per_share = limit - candidate.stop
    planned_risk = candidate.entry - candidate.stop
    paid_up = limit - candidate.entry
    allowance = planned_risk * Decimal(str(max_entry_slippage_r))
    measured["card_risk_reward"] = candidate.risk_reward
    measured["repriced_risk_reward"] = round(float((candidate.target - limit) / risk_per_share), 2)
    measured["paid_above_card"] = str(paid_up)
    measured["planned_risk"] = str(planned_risk)
    measured["max_paid_above_card"] = str(allowance)

    if paid_up > allowance:
        return BuyViability(
            state=DRIFTED,
            buyable=False,
            reasons=("ENTRY_TOO_FAR_ABOVE_CARD",),
            measured=measured,
            as_of=as_of,
        )

    return BuyViability(
        state=LIVE,
        buyable=True,
        reasons=(),
        measured=measured,
        as_of=as_of,
    )


def state_for_reasons(reasons: tuple[str, ...] | list[str]) -> str:
    """Map a gate reason list onto a desk state. First match wins."""
    for reason in reasons:
        mapped = _STATE_FOR_REASON.get(reason)
        if mapped is not None:
            return mapped
    return UNVERIFIED
