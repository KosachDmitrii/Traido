"""Alpaca data feed selection."""

from __future__ import annotations

from core.config import Settings
from market_data.factory import resolve_alpaca_data_feed


def test_defaults_to_iex() -> None:
    s = Settings(ALPACA_DATA_FEED=None)
    assert resolve_alpaca_data_feed(s) == "iex"


def test_explicit_feed_overrides() -> None:
    s = Settings(ALPACA_DATA_FEED="sip")
    assert resolve_alpaca_data_feed(s) == "sip"
