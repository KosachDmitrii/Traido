"""Executable entry spread is always the observed bid/ask width.

A last trade has no executable size or guaranteed freshness. It cannot replace
one side of the quote or turn an uncertain IEX book into a zero-spread market.
"""

from __future__ import annotations

from math import isfinite

from core.schemas import Quote


def book_spread_bps(quote: Quote) -> float | None:
    bid = float(quote.bid)
    ask = float(quote.ask)
    if not isfinite(bid) or not isfinite(ask) or bid <= 0 or ask < bid:
        return None
    mid = bid / 2 + ask / 2
    return (ask - bid) / mid * 10_000.0


def spread_bps_for_entry(
    quote: Quote,
    *,
    last_price: float | None = None,
    feed: str | None = None,
) -> float | None:
    """Keep the shared API; neither feed nor last trade can reduce book width."""
    return book_spread_bps(quote)
