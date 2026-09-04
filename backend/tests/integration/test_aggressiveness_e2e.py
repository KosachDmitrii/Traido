"""Full offer→approve path at each of the five aggressiveness levels."""

from __future__ import annotations

import pytest

from trading.entry_policy import ENTRY_LEVELS, set_entry_aggressiveness


@pytest.mark.parametrize("level", ENTRY_LEVELS)
def test_offer_and_approve_passes_at_each_aggressiveness_level(desk, level: int) -> None:
    """Happy-path capital flow must work on every desk step."""
    set_entry_aggressiveness(level, actor="integration")
    opp = desk.offer("AAPL")

    resp = desk.approve(opp.id)

    assert resp.status_code == 200, f"level={level}: {resp.text}"
    assert len(desk.backend.placed) >= 1


@pytest.mark.parametrize("level", ENTRY_LEVELS)
def test_spread_refusal_uses_same_gate_as_desk(desk, level: int) -> None:
    """Wide spread must refuse approve at every aggressiveness level."""
    from trading.entry_policy import get_entry_thresholds

    set_entry_aggressiveness(level, actor="integration")
    th = get_entry_thresholds()
    cap = th.max_spread_bps
    # Stay below IEX orphan-ask threshold (80 bps above last) but above cap.
    over_cap_bps = min(cap + 10.0, 79.0)
    assert over_cap_bps > cap
    ask = 100.0 * (1.0 + over_cap_bps / 10_000.0)
    desk.market.custom_bid = 99.95
    desk.market.custom_ask = ask
    opp = desk.offer("AAPL")

    resp = desk.approve(opp.id)

    assert resp.status_code in {409, 422}, f"level={level}: {resp.text}"
    detail = resp.json()["detail"]
    assert "SPREAD_TOO_WIDE" in detail or "BUY_REJECTED_SPREAD" in detail, detail
    desk.assert_no_broker_mutations()
