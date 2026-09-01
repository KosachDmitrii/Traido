"""
Pre-execution gates.

Deterministic in, deterministic out. No clock, no network, no model — every
case here pins an exact input to an exact verdict.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from core.enums import BrokerConnectionState, Timeframe
from core.schemas import Bar, Quote
from trading.gates import (
    LiquidityPolicy,
    SpreadSource,
    check_connectivity,
    check_liquidity,
    check_rth,
    measure_spread,
    modeled_spread,
)
from trading.session_hours import (
    SessionPhase,
    early_close_days,
    is_market_holiday,
    market_holidays,
    session_phase,
)

ET = ZoneInfo("America/New_York")


def _bars(
    *,
    count: int = 60,
    close: float = 100.0,
    volume: float = 5_000_000.0,
) -> list[Bar]:
    start = datetime(2026, 1, 2, tzinfo=UTC)
    return [
        Bar(
            symbol="AAPL",
            timeframe=Timeframe.D1,
            ts=start + timedelta(days=i),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=volume,
            source="synthetic",
        )
        for i in range(count)
    ]


NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def _quote(*, bid: float = 99.99, ask: float = 100.01, age_sec: float = 0.0) -> Quote:
    return Quote(
        symbol="AAPL",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=NOW - timedelta(seconds=age_sec),
        source="test",
    )


def _live() -> object:
    return measure_spread(_quote(), now=NOW, max_age_sec=15.0)


# ── Liquidity ────────────────────────────────────────────────────────────────


def test_a_liquid_name_in_normal_size_passes() -> None:
    result = check_liquidity("AAPL", _bars(), qty=Decimal(10), price=Decimal(100), spread=_live())

    assert result.passed
    assert result.reasons == ()
    assert result.measured["avg_dollar_volume"] == pytest.approx(500_000_000.0)
    assert result.measured["spread_source"] == "live"


def test_penny_stocks_are_blocked_by_the_price_floor() -> None:
    result = check_liquidity(
        "PENNY", _bars(close=0.85, volume=500_000_000), qty=Decimal(10), price=Decimal("0.85")
    )

    assert not result.passed
    assert "PRICE_BELOW_FLOOR" in result.reasons


def test_the_default_price_floor_is_ten_dollars() -> None:
    """V1 policy, stated as a test so it cannot drift silently."""
    assert LiquidityPolicy().min_price == Decimal(10)

    result = check_liquidity(
        "CHEAP", _bars(close=9.5, volume=500_000_000), qty=Decimal(10), price=Decimal("9.50")
    )
    assert "PRICE_BELOW_FLOOR" in result.reasons


def test_illiquid_names_are_blocked() -> None:
    result = check_liquidity("THIN", _bars(volume=1_000), qty=Decimal(10), price=Decimal(100))

    assert not result.passed
    assert "INSUFFICIENT_AVG_DOLLAR_VOLUME" in result.reasons
    assert "INSUFFICIENT_CURRENT_VOLUME" in result.reasons


def test_a_wide_spread_is_blocked_when_it_can_be_measured() -> None:
    wide = measure_spread(_quote(bid=99.0, ask=101.0), now=NOW, max_age_sec=15.0)
    result = check_liquidity("AAPL", _bars(), qty=Decimal(10), price=Decimal(100), spread=wide)

    assert not result.passed
    assert "SPREAD_TOO_WIDE" in result.reasons


# ── Spread honesty ───────────────────────────────────────────────────────────
#
# The failure this section exists to prevent: reporting "spread OK" when no
# spread was ever observed. A number we did not measure is not a passed check.


def test_a_fresh_quote_yields_a_live_spread() -> None:
    reading = measure_spread(_quote(bid=99.99, ask=100.01), now=NOW, max_age_sec=15.0)

    assert reading.source is SpreadSource.LIVE
    assert reading.bps == pytest.approx(2.0, abs=0.01)


def test_a_stale_quote_is_not_a_live_spread() -> None:
    reading = measure_spread(_quote(age_sec=120.0), now=NOW, max_age_sec=15.0)

    assert reading.source is SpreadSource.STALE
    assert reading.bps is not None, "the number is still reported, it is just not trusted"


def test_a_missing_quote_is_unavailable_not_zero() -> None:
    reading = measure_spread(None, now=NOW, max_age_sec=15.0)

    assert reading.source is SpreadSource.UNAVAILABLE
    assert reading.bps is None


def test_a_crossed_book_is_treated_as_unavailable() -> None:
    reading = measure_spread(_quote(bid=101.0, ask=99.0), now=NOW, max_age_sec=15.0)

    assert reading.source is SpreadSource.UNAVAILABLE


def test_a_live_entry_fails_closed_without_a_quote() -> None:
    """The V1 default. Absent live top of book, the entry does not go."""
    result = check_liquidity("AAPL", _bars(), qty=Decimal(10), price=Decimal(100))

    assert not result.passed
    assert "LIVE_QUOTE_REQUIRED" in result.reasons


def test_a_live_entry_fails_closed_on_a_stale_quote() -> None:
    stale = measure_spread(_quote(age_sec=300.0), now=NOW, max_age_sec=15.0)
    result = check_liquidity("AAPL", _bars(), qty=Decimal(10), price=Decimal(100), spread=stale)

    assert not result.passed
    assert "QUOTE_STALE" in result.reasons


def test_a_modeled_spread_never_satisfies_a_live_gate() -> None:
    result = check_liquidity(
        "AAPL", _bars(), qty=Decimal(10), price=Decimal(100), spread=modeled_spread(2.0)
    )

    assert not result.passed
    assert "LIVE_QUOTE_REQUIRED" in result.reasons
    assert result.measured["spread_source"] == "modeled"


def test_research_mode_may_use_a_modeled_spread_but_it_stays_labelled() -> None:
    """Backtests need a spread assumption; they just may not call it observed."""
    policy = LiquidityPolicy(require_live_spread=False)
    result = check_liquidity(
        "AAPL",
        _bars(),
        qty=Decimal(10),
        price=Decimal(100),
        spread=modeled_spread(2.0),
        policy=policy,
    )

    assert result.passed
    assert result.measured["spread_source"] == "modeled"


# ── Broker connectivity ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state",
    [
        BrokerConnectionState.DISCONNECTED,
        BrokerConnectionState.CONNECTING,
        BrokerConnectionState.DEGRADED,
        BrokerConnectionState.RECONNECTING,
    ],
)
def test_only_a_ready_broker_may_change_exposure(state: BrokerConnectionState) -> None:
    assert not check_connectivity(state).passed
    assert check_connectivity(BrokerConnectionState.READY).passed


def test_an_order_too_large_for_the_tape_is_blocked() -> None:
    result = check_liquidity("AAPL", _bars(), qty=Decimal(200_000), price=Decimal(100))

    assert not result.passed
    assert "ORDER_TOO_LARGE_FOR_LIQUIDITY" in result.reasons
    assert result.measured["participation_pct"] > LiquidityPolicy().max_participation_pct


def test_otc_is_blocked_through_universe_membership() -> None:
    """OTC names are never listed, so membership is the deterministic test."""
    policy = LiquidityPolicy(allowed_symbols=frozenset({"AAPL", "MSFT"}))

    result = check_liquidity("OTCXX", _bars(), qty=Decimal(10), price=Decimal(100), policy=policy)

    assert not result.passed
    assert "SYMBOL_NOT_IN_UNIVERSE" in result.reasons


def test_too_little_history_fails_closed() -> None:
    result = check_liquidity("NEW", _bars(count=5), qty=Decimal(10), price=Decimal(100))

    assert not result.passed
    assert "INSUFFICIENT_HISTORY" in result.reasons


def test_the_result_reports_measurements_and_thresholds() -> None:
    result = check_liquidity("AAPL", _bars(), qty=Decimal(10), price=Decimal(100))
    payload = result.as_dict()

    assert payload["gate"] == "liquidity"
    assert payload["thresholds"]["min_price"] == "10"
    assert payload["measured"]["notional"] == 1000.0


# ── Regular trading hours ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("moment", "phase"),
    [
        (datetime(2026, 3, 10, 11, 0, tzinfo=ET), SessionPhase.REGULAR),
        (datetime(2026, 3, 10, 9, 30, tzinfo=ET), SessionPhase.REGULAR),
        (datetime(2026, 3, 10, 15, 59, tzinfo=ET), SessionPhase.REGULAR),
        (datetime(2026, 3, 10, 8, 0, tzinfo=ET), SessionPhase.PREMARKET),
        (datetime(2026, 3, 10, 16, 0, tzinfo=ET), SessionPhase.AFTER_HOURS),
        (datetime(2026, 3, 10, 20, 0, tzinfo=ET), SessionPhase.AFTER_HOURS),
        (datetime(2026, 3, 14, 11, 0, tzinfo=ET), SessionPhase.CLOSED_WEEKEND),  # Saturday
        (datetime(2026, 3, 15, 11, 0, tzinfo=ET), SessionPhase.CLOSED_WEEKEND),  # Sunday
        (datetime(2026, 12, 25, 11, 0, tzinfo=ET), SessionPhase.CLOSED_HOLIDAY),
        # 4 July 2026 is a Saturday, so the 3rd is the observed holiday.
        (datetime(2026, 7, 3, 11, 0, tzinfo=ET), SessionPhase.CLOSED_HOLIDAY),
        (datetime(2026, 12, 24, 12, 0, tzinfo=ET), SessionPhase.REGULAR),  # half day, still open
    ],
)
def test_session_phase_classification(moment: datetime, phase: SessionPhase) -> None:
    assert session_phase(moment) is phase


def test_new_entries_are_blocked_outside_the_regular_session() -> None:
    blocked = {
        "premarket": datetime(2026, 3, 10, 8, 0, tzinfo=ET),
        "after_hours": datetime(2026, 3, 10, 17, 0, tzinfo=ET),
        "weekend": datetime(2026, 3, 14, 11, 0, tzinfo=ET),
        "holiday": datetime(2026, 12, 25, 11, 0, tzinfo=ET),
    }
    for label, moment in blocked.items():
        result = check_rth(moment)
        assert not result.passed, label
        assert result.reasons, label

    assert check_rth(datetime(2026, 3, 10, 11, 0, tzinfo=ET)).passed


def test_an_early_close_shortens_the_session() -> None:
    """13:00 ET on a half day is after hours, even though 13:00 normally is not."""
    christmas_eve = datetime(2026, 12, 24, 13, 30, tzinfo=ET)

    assert christmas_eve.date() in early_close_days(2026)
    assert session_phase(christmas_eve) is SessionPhase.AFTER_HOURS
    assert session_phase(christmas_eve.replace(hour=12)) is SessionPhase.REGULAR


def test_the_holiday_calendar_computes_the_movable_dates() -> None:
    holidays = market_holidays(2026)

    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 5, 25) in holidays  # Memorial Day
    assert date(2026, 11, 26) in holidays  # Thanksgiving
    assert date(2026, 1, 19) in holidays  # MLK Day


def test_a_holiday_falling_at_the_weekend_is_observed_on_a_weekday() -> None:
    # 4 July 2026 is a Saturday, so the market closes on Friday the 3rd.
    assert is_market_holiday(date(2026, 7, 3))
    assert not is_market_holiday(date(2026, 7, 6))
