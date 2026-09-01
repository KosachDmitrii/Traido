"""A standing BUY card must show whether the live book still clears it.

The hour-long TTL keeps the proposal on the desk. The entry gate still refuses
a drifted or wide book at click time. Without a preview those two facts
together produce a BUY button that looks live and then toast-rejects — which
is how the operator learned, after the press, that XOM and CVX were already
past 0.25R above the card.

These tests pin the geometry shared by the desk preview and decide. They do
not place orders and they do not withdraw cards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.enums import TradeAction
from core.schemas import Quote, TradeCandidate
from trading.gates import SpreadReading, SpreadSource
from trading.viability import (
    DRIFTED,
    LIVE,
    PAST_SETUP,
    UNVERIFIED,
    WIDE,
    assess_buy_viability,
)

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def _candidate(
    *,
    entry: str = "100",
    stop: str = "98",
    target: str = "104",
) -> TradeCandidate:
    return TradeCandidate(
        symbol="TEST",
        action=TradeAction.BUY,
        entry=Decimal(entry),
        stop=Decimal(stop),
        target=Decimal(target),
        confidence=0.7,
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
    )


def _quote(bid: str, ask: str, *, age_sec: float = 1.0) -> Quote:
    return Quote(
        symbol="TEST",
        bid=Decimal(bid),
        ask=Decimal(ask),
        ts=NOW,
        source="test",
    )


def _live(bid: str, ask: str) -> SpreadReading:
    return SpreadReading(source=SpreadSource.LIVE, bps=float((Decimal(ask) - Decimal(bid)) / ((Decimal(ask) + Decimal(bid)) / 2) * 10_000), age_sec=1.0)


def test_a_tight_book_at_the_card_is_live() -> None:
    v = assess_buy_viability(
        _candidate(),
        _quote("99.98", "100.02"),
        spread=_live("99.98", "100.02"),
        now=NOW,
    )
    assert v.state == LIVE
    assert v.buyable is True
    assert v.reasons == ()


def test_a_wide_spread_locks_buy_without_calling_it_drift() -> None:
    """400bps is a book problem, not an entry-slippage problem."""
    v = assess_buy_viability(
        _candidate(),
        _quote("98", "102"),
        spread=SpreadReading(source=SpreadSource.LIVE, bps=400.0, age_sec=1.0),
        now=NOW,
    )
    assert v.state == WIDE
    assert v.buyable is False
    assert "SPREAD_TOO_WIDE" in v.reasons


def test_paying_more_than_a_quarter_r_is_drifted() -> None:
    # Card risk = 2.00. Allowance = 0.50. Ask 100.60 → limit ~100.70 > 100.50.
    v = assess_buy_viability(
        _candidate(entry="100", stop="98", target="104"),
        _quote("100.55", "100.60"),
        spread=_live("100.55", "100.60"),
        now=NOW,
    )
    assert v.state == DRIFTED
    assert v.buyable is False
    assert "ENTRY_TOO_FAR_ABOVE_CARD" in v.reasons


def test_a_limit_through_the_target_is_past_setup() -> None:
    v = assess_buy_viability(
        _candidate(entry="100", stop="98", target="100.5"),
        _quote("100.40", "100.55"),
        spread=_live("100.40", "100.55"),
        now=NOW,
    )
    assert v.state == PAST_SETUP
    assert v.buyable is False
    assert "PRICE_MOVED_PAST_SETUP" in v.reasons


def test_a_missing_quote_is_unverified_not_live() -> None:
    v = assess_buy_viability(_candidate(), None, now=NOW)
    assert v.state == UNVERIFIED
    assert v.buyable is False
    assert "LIVE_QUOTE_REQUIRED" in v.reasons


def test_a_stale_quote_is_unverified() -> None:
    v = assess_buy_viability(
        _candidate(),
        _quote("99.98", "100.02"),
        spread=SpreadReading(source=SpreadSource.STALE, bps=4.0, age_sec=60.0),
        now=NOW,
    )
    assert v.state == UNVERIFIED
    assert v.buyable is False
    assert "QUOTE_STALE" in v.reasons
