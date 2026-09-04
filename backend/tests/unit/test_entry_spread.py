"""Entry spread measurement for IEX stale bids."""

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


def test_iex_uses_buy_friction_when_last_inside_book() -> None:
    q = _quote(176.5, 178.55)
    bps = spread_bps_for_entry(q, last_price=178.455, feed="iex")
    assert bps == pytest.approx(5.4, rel=0.05)


def test_sip_keeps_full_book_spread() -> None:
    q = _quote(176.5, 178.55)
    bps = spread_bps_for_entry(q, last_price=178.455, feed="sip")
    assert bps == pytest.approx(115.0, rel=0.02)


def test_iex_outside_book_uses_book() -> None:
    q = _quote(72.0, 72.8)
    bps = spread_bps_for_entry(q, last_price=71.5, feed="iex")
    assert bps == pytest.approx(110.0, rel=0.02)


def test_iex_last_above_ask_zero_friction() -> None:
    # Tape ticked above a tight IEX offer — book width would false-block.
    q = _quote(159.0, 162.20)
    bps = spread_bps_for_entry(q, last_price=162.23, feed="iex")
    assert bps == pytest.approx(0.0, abs=0.1)


def test_iex_orphan_ask_above_tape_zero_friction() -> None:
    # Live XOM IEX artifact: stale low bid + orphan high ask, tape between.
    q = _quote(155.8, 173.22)
    bps = spread_bps_for_entry(q, last_price=162.23, feed="iex")
    assert bps == pytest.approx(0.0, abs=0.1)
