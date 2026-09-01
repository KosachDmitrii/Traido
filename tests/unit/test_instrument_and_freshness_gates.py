"""P1-4 and P1-5 at the gate level.

Both gates are wired into the entry chain and proved end to end in
`tests/integration/test_entry_gates_end_to_end.py`. These cover the cases that
are about the rule itself rather than about the wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.enums import Timeframe
from core.schemas import Bar
from trading.gates import check_bar_freshness, check_instrument_eligibility

NOW = datetime(2026, 6, 15, 15, 0, tzinfo=UTC)


def _bars(*, newest_age_days: float, count: int = 30) -> list[Bar]:
    newest = NOW - timedelta(days=newest_age_days)
    return [
        Bar(
            symbol="AAPL",
            timeframe=Timeframe.D1,
            ts=newest - timedelta(days=i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=5_000_000.0,
            source="test",
        )
        for i in range(count)
    ]


# ── Freshness ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("age_days", [0, 1, 3, 4.9])
def test_an_ordinary_gap_passes(age_days: float) -> None:
    """Daily bars stop over weekends and holidays; refusing those trains people
    to widen the threshold until it means nothing."""
    assert check_bar_freshness("AAPL", _bars(newest_age_days=age_days), now=NOW).passed


@pytest.mark.parametrize("age_days", [5.1, 8, 30])
def test_a_feed_that_has_stopped_is_refused(age_days: float) -> None:
    result = check_bar_freshness("AAPL", _bars(newest_age_days=age_days), now=NOW)
    assert not result.passed
    assert result.reasons == ("STALE_BARS",)


def test_an_empty_series_is_refused_and_named_separately() -> None:
    """No data and old data are different operational problems."""
    result = check_bar_freshness("AAPL", [], now=NOW)
    assert not result.passed
    assert result.reasons == ("NO_BARS",)


def test_the_newest_bar_decides_not_the_last_in_the_list() -> None:
    """Order is a vendor's choice, not a fact about the data."""
    bars = _bars(newest_age_days=0)
    assert check_bar_freshness("AAPL", list(reversed(bars)), now=NOW).passed


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing() -> None:
    bars = [
        b.model_copy(update={"ts": b.ts.replace(tzinfo=None)}) for b in _bars(newest_age_days=1)
    ]
    assert check_bar_freshness("AAPL", bars, now=NOW).passed


# ── Eligibility ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "F", "BRK.B"])
def test_a_listed_equity_passes(symbol: str) -> None:
    assert check_instrument_eligibility(symbol).passed


@pytest.mark.parametrize("symbol", ["ABCDF", "WXYZY"])
def test_an_otc_shaped_ticker_is_refused_when_no_security_type_is_known(symbol: str) -> None:
    """The Alpaca backstop. Crude by design, and only used where nothing better exists."""
    result = check_instrument_eligibility(symbol)
    assert not result.passed
    assert "SYMBOL_LOOKS_OTC" in result.reasons


def test_a_known_security_type_overrides_the_shape_heuristic() -> None:
    """On a path that resolves a real contract, guessing from the ticker is wrong."""
    assert check_instrument_eligibility("ABCDF", security_type="STK", currency="USD").passed


def test_a_non_equity_is_refused() -> None:
    result = check_instrument_eligibility("ES", security_type="FUT", currency="USD")
    assert "SECURITY_TYPE_NOT_EQUITY" in result.reasons


def test_a_foreign_currency_is_refused() -> None:
    result = check_instrument_eligibility("BMW", security_type="STK", currency="EUR")
    assert "CURRENCY_NOT_USD" in result.reasons


def test_a_symbol_outside_the_allow_list_is_refused() -> None:
    result = check_instrument_eligibility("AAPL", allowed_symbols=frozenset({"MSFT"}))
    assert "SYMBOL_NOT_ALLOWED" in result.reasons


def test_an_option_style_symbol_is_refused() -> None:
    result = check_instrument_eligibility("AAPL260619C00150000")
    assert not result.passed


def test_every_failing_rule_is_reported_at_once() -> None:
    """An operator reading a refusal wants the whole story, not the first line of it."""
    result = check_instrument_eligibility(
        "BMW", security_type="FUT", currency="EUR", allowed_symbols=frozenset({"AAPL"})
    )
    assert set(result.reasons) == {
        "SYMBOL_NOT_ALLOWED",
        "SECURITY_TYPE_NOT_EQUITY",
        "CURRENCY_NOT_USD",
    }
