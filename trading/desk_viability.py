"""Attach live BUY viability to desk cards.

The light desk payload is otherwise a journal of standing proposals. Without a
viability reading the BUY button looks live for the whole hour-long TTL, and the
operator discovers a wide book or a drifted entry only after pressing it. That
is the wrong moment to learn the card is not a trade.

Read quotes for the open buy cards, run `assess_buy_viability`, and hang the
result on each card. Never withdraws: a wide book comes back, and deleting the
proposal would be the opposite of what the desk needs. Cap the work — five open
buys is the queue ceiling — and cache briefly so a 2-second desk poll does not
become a quote storm.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from core.config import get_settings
from core.schemas import TradeOpportunity
from market_data.factory import create_market_data_port
from trading.gates import LiquidityPolicy, measure_spread
from trading.viability import UNVERIFIED, BuyViability, assess_buy_viability

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 5.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _unverified(reason: str = "LIVE_QUOTE_REQUIRED") -> dict[str, Any]:
    return BuyViability(
        state=UNVERIFIED,
        buyable=False,
        reasons=(reason,),
        measured={},
        as_of=datetime.now(UTC),
    ).as_dict()


async def attach_buy_viability(
    opportunities: list[TradeOpportunity],
    *,
    quotes: Any | None = None,
) -> list[dict[str, Any]]:
    """Return opportunity dicts with a `viability` field each.

    Failures to read a quote are `unverified` and not buyable — the same
    fail-closed posture as the entry gate. A missing market-data port is the
    same: the desk must not claim a card is live when it cannot look.
    """
    if not opportunities:
        return []

    feed = quotes
    if feed is None:
        try:
            feed = create_market_data_port(get_settings())
        except Exception:
            logger.warning("buy viability: market data unavailable", exc_info=True)
            feed = None

    now_mono = time.monotonic()

    async def _one(opp: TradeOpportunity) -> dict[str, Any]:
        payload = opp.model_dump(mode="json")
        key = str(opp.id)
        cached = _cache.get(key)
        if cached is not None and (now_mono - cached[0]) < _CACHE_TTL_SEC:
            payload["viability"] = cached[1]
            return payload

        if feed is None or not hasattr(feed, "get_quote"):
            viability = _unverified("MARKET_DATA_NOT_CONFIGURED")
            _cache[key] = (now_mono, viability)
            payload["viability"] = viability
            return payload

        try:
            quote = await feed.get_quote(opp.candidate.symbol)
        except Exception:
            logger.warning(
                "buy viability: quote failed for %s", opp.candidate.symbol, exc_info=True
            )
            viability = _unverified("LIVE_QUOTE_REQUIRED")
            _cache[key] = (now_mono, viability)
            payload["viability"] = viability
            return payload

        spread = measure_spread(
            quote,
            now=datetime.now(UTC),
            max_age_sec=LiquidityPolicy().max_quote_age_sec,
        )
        reading = assess_buy_viability(opp.candidate, quote, spread=spread)
        viability = reading.as_dict()
        _cache[key] = (now_mono, viability)
        payload["viability"] = viability
        return payload

    # Bounded: the buy queue itself is capped at five. Gather keeps the desk
    # poll from serialising five quote round-trips behind a single operator.
    return list(await asyncio.gather(*(_one(opp) for opp in opportunities)))


def clear_viability_cache() -> None:
    """Tests only — drop the process cache between cases."""
    _cache.clear()
