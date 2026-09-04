"""Spread measurement for entry gates.

SIP/NBBO bid/ask is a consolidated book. Alpaca IEX is a single exchange: the
bid can sit far below the last print while the ask tracks the market. Using the
full book spread false-blocks liquid names (e.g. ZS ~115 bps book vs ~6 bps to
lift the offer from last).

For IEX buy entries, spread is the cost to lift the offer vs the last print —
not bid-to-ask width. IEX often shows a stale low bid (ZS) or a stale high ask
(XOM) while the tape trades between them.
"""

from __future__ import annotations

from core.schemas import Quote

# Above this gap from last to ask, treat the IEX offer as orphan/stale when the
# tape already prints above the IEX bid (~0.8% — far beyond a liquid NBBO lift).
_STALE_IEX_ASK_ABOVE_LAST_BPS = 80.0


def book_spread_bps(quote: Quote) -> float | None:
    bid = float(quote.bid)
    ask = float(quote.ask)
    if bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10_000.0


def spread_bps_for_entry(
    quote: Quote,
    *,
    last_price: float | None = None,
    feed: str | None = None,
) -> float | None:
    """Spread relevant to a buy fill — feed-aware for IEX stale bids."""
    book = book_spread_bps(quote)
    if book is None:
        return None

    feed_key = (feed or "iex").strip().lower()
    if feed_key != "iex" or last_price is None or last_price <= 0:
        return book

    last = float(last_price)
    bid = float(quote.bid)
    ask = float(quote.ask)

    if last >= ask:
        buy_bps = 0.0
    else:
        ask_above_last_bps = (ask - last) / last * 10_000.0
        if ask_above_last_bps > _STALE_IEX_ASK_ABOVE_LAST_BPS and last > bid:
            # Orphan ask above a tape that already cleared the IEX bid — lift
            # would be near last, not at the stale offer (live XOM artifact).
            buy_bps = 0.0
        else:
            buy_bps = max(0.0, ask_above_last_bps)
    return min(book, buy_bps)
