"""Keep top-of-book provenance independent of the last-trade clock."""

from __future__ import annotations

from datetime import datetime

from core.schemas import Quote


def quote_with_trade_freshness(
    quote: Quote | None,
    *,
    trade_ts: datetime | None,
) -> Quote | None:
    """A trade cannot refresh bid/ask. Preserve the API without inventing freshness."""
    return quote
