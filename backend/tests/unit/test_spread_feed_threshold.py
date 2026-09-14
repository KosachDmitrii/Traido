"""Spread thresholds are calibrated only for Alpaca SIP."""

from __future__ import annotations

import pytest

from market_data.spread_threshold import max_spread_bps_for_feed
from trading.entry_policy import get_entry_thresholds, reset_entry_policy_cache, thresholds_for


def test_sip_keeps_base_spread_bands() -> None:
    assert max_spread_bps_for_feed(30.0, "sip") == pytest.approx(30.0)
    assert max_spread_bps_for_feed(42.0, "sip") == pytest.approx(42.0)


def test_non_sip_feed_is_rejected() -> None:
    with pytest.raises(ValueError, match="ALPACA_SIP_FEED_REQUIRED"):
        max_spread_bps_for_feed(42.0, "other")


def test_get_entry_thresholds_use_sip_in_production(monkeypatch) -> None:
    reset_entry_policy_cache()
    monkeypatch.setenv("ALPACA_DATA_FEED", "sip")
    from core.config import get_settings

    get_settings.cache_clear()
    th_sip = get_entry_thresholds()
    assert th_sip.max_spread_bps == pytest.approx(
        thresholds_for(th_sip.aggressiveness).max_spread_bps
    )
