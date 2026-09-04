"""Unified entry spread gate — desk and approve must agree."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

import pytest

from core.schemas import Quote
from market_data.factory import resolve_alpaca_data_feed
from tests.conftest import RTH_INSTANT
from trading.entry_policy import get_entry_thresholds, set_entry_aggressiveness, thresholds_for
from trading.entry_spread_gate import evaluate_entry_spread, resolve_spread_reference_price


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        symbol="AAPL",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=RTH_INSTANT.astimezone(UTC),
        source="test",
    )


def test_resolve_reference_prefers_tape_last() -> None:
    q = _quote(90.0, 100.01)
    assert resolve_spread_reference_price(q, tape_last=100.0, card_entry=99.0) == 100.0


def test_iex_uses_buy_friction_not_book_width() -> None:
    q = _quote(90.0, 100.01)
    now = RTH_INSTANT.astimezone(UTC)
    th = thresholds_for(0)
    gate = evaluate_entry_spread(
        q,
        now=now,
        tape_last=100.0,
        thresholds=th,
        feed="iex",
    )
    assert gate.bps is not None
    assert gate.bps < 5.0
    assert gate.acceptable is True


def test_desk_and_admission_share_same_gate_at_weak_level() -> None:
    set_entry_aggressiveness(100, actor="test")
    q = _quote(99.95, 100.05)
    now = RTH_INSTANT.astimezone(UTC)
    th = get_entry_thresholds()
    gate = evaluate_entry_spread(q, now=now, tape_last=100.0, thresholds=th, feed="iex")
    assert gate.max_bps == pytest.approx(70.0)
    assert gate.acceptable is True


def test_paper_defaults_to_iex_live_defaults_to_sip(monkeypatch) -> None:
    from core.config import Settings

    paper = Settings(TRAIDO_BROKER_ENV="paper", ALPACA_DATA_FEED=None)
    live = Settings(TRAIDO_BROKER_ENV="live", ALPACA_DATA_FEED=None)
    assert resolve_alpaca_data_feed(paper) == "iex"
    assert resolve_alpaca_data_feed(live) == "sip"


def test_explicit_feed_overrides_broker_env(monkeypatch) -> None:
    from core.config import Settings

    s = Settings(TRAIDO_BROKER_ENV="live", ALPACA_DATA_FEED="iex")
    assert resolve_alpaca_data_feed(s) == "iex"
