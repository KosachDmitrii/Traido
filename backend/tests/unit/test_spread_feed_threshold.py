"""Spread threshold scales with Alpaca data feed."""

from __future__ import annotations

import pytest

from market_data.spread_threshold import max_spread_bps_for_feed
from trading.entry_policy import get_entry_thresholds, reset_entry_policy_cache, thresholds_for


def test_sip_keeps_base_spread_bands() -> None:
    assert max_spread_bps_for_feed(30.0, "sip") == pytest.approx(30.0)
    assert max_spread_bps_for_feed(42.0, "sip") == pytest.approx(42.0)


def test_iex_widens_weak_step_to_70_bps() -> None:
    assert max_spread_bps_for_feed(42.0, "iex") == pytest.approx(70.0)
    assert max_spread_bps_for_feed(30.0, "iex") == pytest.approx(50.0)
    assert max_spread_bps_for_feed(35.0, "iex") == pytest.approx(58.3)


def test_get_entry_thresholds_respects_feed(monkeypatch) -> None:
    reset_entry_policy_cache()
    monkeypatch.setenv("ALPACA_DATA_FEED", "sip")
    from core.config import get_settings

    get_settings.cache_clear()
    th_sip = get_entry_thresholds()
    assert th_sip.max_spread_bps == pytest.approx(
        thresholds_for(th_sip.aggressiveness).max_spread_bps
    )

    monkeypatch.setenv("ALPACA_DATA_FEED", "iex")
    get_settings.cache_clear()
    reset_entry_policy_cache()
    th_iex = get_entry_thresholds()
    base = thresholds_for(th_iex.aggressiveness).max_spread_bps
    assert th_iex.max_spread_bps == pytest.approx(max_spread_bps_for_feed(base, "iex"))
    assert th_iex.max_spread_bps > th_sip.max_spread_bps or base == th_sip.max_spread_bps
