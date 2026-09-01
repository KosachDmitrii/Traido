"""Unit tests for Alpaca equity price rounding."""

from decimal import Decimal

from trading.pricing import format_qty, round_equity_price, round_equity_qty, round_order_qty


def test_round_equity_price_penny_tick() -> None:
    assert round_equity_price(Decimal("310.0113")) == Decimal("310.01")
    assert round_equity_price("99.995") == Decimal("100.00")


def test_round_equity_price_subdollar() -> None:
    assert round_equity_price(Decimal("0.12345")) == Decimal("0.1235")


def test_round_equity_qty() -> None:
    assert round_equity_qty(Decimal("1.234567")) == Decimal("1.2345")


def test_round_order_qty_floors_to_whole_shares() -> None:
    assert round_order_qty(Decimal("75.2219")) == Decimal(75)
    assert round_order_qty(Decimal("0.94")) == Decimal(0)


def test_round_order_qty_never_rounds_up() -> None:
    # Rounding up would size above what risk approved.
    assert round_order_qty(Decimal("75.9999")) == Decimal(75)


def test_format_qty_sends_a_whole_number_as_whole() -> None:
    assert format_qty(Decimal("50.0000")) == "50"
    assert format_qty(Decimal(50)) == "50"


def test_format_qty_never_uses_exponent_notation() -> None:
    # `Decimal.normalize` alone turns fifty into 5E+1, which no venue reads.
    assert format_qty(Decimal("5000.0000")) == "5000"


def test_format_qty_keeps_a_real_fraction() -> None:
    assert format_qty(Decimal("0.5000")) == "0.5"
    assert format_qty(Decimal("75.2219")) == "75.2219"


def test_round_equity_qty_keeps_a_reported_fraction() -> None:
    # A fill the venue reports is protected for exactly that quantity, so this
    # rounding must not discard the fraction the way `round_order_qty` does.
    assert round_equity_qty(Decimal("75.2219")) == Decimal("75.2219")
