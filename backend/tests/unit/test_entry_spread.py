"""Entry spread never substitutes tape prices for executable quotes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.schemas import Quote
from market_data.entry_spread import book_spread_bps, spread_bps_for_entry


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        symbol="ZS",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC),
        source="test",
    )


def test_book_spread_zs_iex_example() -> None:
    # Realistic IEX artifact: stale low bid, ask near last.
    q = _quote(176.5, 178.55)
    assert book_spread_bps(q) == pytest.approx(115.0, rel=0.02)


def test_iex_keeps_book_when_last_inside_book() -> None:
    q = _quote(176.5, 178.55)
    bps = spread_bps_for_entry(q, last_price=178.455, feed="iex")
    assert bps == pytest.approx(book_spread_bps(q))


def test_sip_keeps_full_book_spread() -> None:
    q = _quote(176.5, 178.55)
    bps = spread_bps_for_entry(q, last_price=178.455, feed="sip")
    assert bps == pytest.approx(115.0, rel=0.02)


def test_iex_outside_book_uses_book() -> None:
    q = _quote(72.0, 72.8)
    bps = spread_bps_for_entry(q, last_price=71.5, feed="iex")
    assert bps == pytest.approx(110.0, rel=0.02)


def test_iex_last_above_ask_does_not_zero_spread() -> None:
    # A last print above the ask does not prove the quote is executable.
    q = _quote(159.0, 162.20)
    bps = spread_bps_for_entry(q, last_price=162.23, feed="iex")
    assert bps == pytest.approx(book_spread_bps(q))


def test_iex_orphan_ask_does_not_zero_spread() -> None:
    # Live XOM IEX artifact: stale low bid + orphan high ask, tape between.
    q = _quote(155.8, 173.22)
    bps = spread_bps_for_entry(q, last_price=162.23, feed="iex")
    assert bps == pytest.approx(book_spread_bps(q))


@pytest.mark.parametrize("last", [None, 318.55, 320.97, 321.21, float("nan")])
def test_axp_screenshot_wide_quote_cannot_pass_via_tape(last):
    q = _quote(318.55, 320.98)
    assert spread_bps_for_entry(q, last_price=last, feed="iex") == pytest.approx(75.9933, rel=1e-5)


@pytest.mark.parametrize("bid,ask", [(float("nan"), 100), (99, float("inf")), (101, 100), (0, 100)])
def test_invalid_book_is_unavailable(bid, ask):
    q = _quote(99, 100).model_copy(update={"bid": Decimal(str(bid)), "ask": Decimal(str(ask))})
    assert spread_bps_for_entry(q, last_price=100, feed="iex") is None
