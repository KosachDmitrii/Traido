"""Fresh trades must not disguise stale top-of-book data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.schemas import Quote
from market_data.quote_freshness import quote_with_trade_freshness


def test_quote_ts_preserved_with_fresher_trade() -> None:
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
    assert out is quote
    assert out.ts == quote.ts


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


@pytest.mark.parametrize("trade_ts", [None, datetime(2099, 1, 1, tzinfo=UTC), datetime(2026, 1, 1)])  # noqa: DTZ001 — invalid input regression
def test_missing_or_invalid_trade_clock_cannot_change_quote(trade_ts) -> None:
    quote = Quote(
        symbol="XOM", bid=100, ask=101, ts=datetime(2026, 1, 1, tzinfo=UTC), source="test"
    )
    assert quote_with_trade_freshness(quote, trade_ts=trade_ts) is quote


def test_trade_cannot_create_missing_quote() -> None:
    assert quote_with_trade_freshness(None, trade_ts=datetime.now(UTC)) is None
