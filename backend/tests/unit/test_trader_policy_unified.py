"""Trader gates derive from EntryThresholds — one slider, one picture."""

from __future__ import annotations

import pytest

from agents.trader.policy import trader_gates_for
from trading.entry_policy import thresholds_for


@pytest.mark.parametrize("level", [0, 25, 50, 75, 100])
def test_trader_gates_match_entry_thresholds(level: int) -> None:
    th = thresholds_for(level)
    policy = trader_gates_for(th)
    assert policy.aggressiveness == level
    assert policy.require_uptrend is th.require_uptrend
    assert policy.allow_range is th.allow_range
    assert policy.require_ema_stack is th.require_ema_stack
    assert policy.rsi_overbought == th.rsi_overbought
    assert policy.chase_ext_frac == th.chase_ext_frac
    assert policy.near_sma_frac == th.near_sma_frac
    assert policy.allow_below_sma is th.allow_below_sma
