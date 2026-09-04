"""Quote freshness alignment with last trade."""

from __future__ import annotations

from datetime import UTC, datetime

from core.schemas import Quote
from market_data.quote_freshness import quote_with_trade_freshness


def test_quote_ts_bumps_to_fresher_trade() -> None:
    quote = Quote(
        symbol="XOM",
        bid=155.8,
        ask=173.22,
        ts=datetime(2026, 9, 3, 20, 0, 0, tzinfo=UTC),
        source="test",
    )
    trade_ts = datetime(2026, 9, 3, 20, 9, 55, tzinfo=UTC)
    out = quote_with_trade_freshness(quote, trade_ts=trade_ts)
    assert out is not None
    assert out.ts == trade_ts


def test_quote_ts_unchanged_when_trade_older() -> None:
    quote = Quote(
        symbol="XOM",
        bid=162.0,
        ask=162.2,
        ts=datetime(2026, 9, 3, 20, 9, 55, tzinfo=UTC),
        source="test",
    )
    trade_ts = datetime(2026, 9, 3, 20, 9, 50, tzinfo=UTC)
    out = quote_with_trade_freshness(quote, trade_ts=trade_ts)
    assert out is not None
    assert out.ts == quote.ts
