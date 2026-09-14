"""Alpaca data feed selection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Settings
from market_data.factory import resolve_alpaca_data_feed


def test_paper_defaults_to_consolidated_sip() -> None:
    s = Settings(TRAIDO_BROKER_ENV="paper")
    assert resolve_alpaca_data_feed(s) == "sip"


def test_explicit_sip_is_accepted() -> None:
    s = Settings(ALPACA_DATA_FEED="sip")
    assert resolve_alpaca_data_feed(s) == "sip"


def test_iex_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(ALPACA_DATA_FEED="iex")
