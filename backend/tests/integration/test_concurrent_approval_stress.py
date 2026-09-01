"""Stress: many concurrent approvals must produce one broker BUY.

Pins the DB compare-and-swap on opportunity claim and intent SUBMITTING.
Do not weaken the assertion if this flakes — find the race.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest


def _in_parallel(calls: list) -> list:
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [f.result() for f in [pool.submit(c) for c in calls]]


@pytest.mark.parametrize("workers", [2, 4, 8])
def test_n_simultaneous_approvals_place_one_order(desk, workers: int) -> None:
    opp = desk.offer("AAPL")
    results = _in_parallel([lambda oid=opp.id: desk.approve(oid) for _ in range(workers)])
    buys = [m for m in desk.backend.placed if m.side == "buy"]
    assert len(buys) == 1, f"{workers} approvals → {len(buys)} buys: {buys}"
    assert any(r.status_code == 200 for r in results)


def test_fifty_rounds_of_dual_approval_never_double(desk) -> None:
    """Fifty dual-clicks: never two BUYs for one card (success optional per round).

    After a fill the book holds AAPL, so later rounds may both refuse —
    that is capacity, not a race. The invariant under test is `delta <= 1`.
    """
    from trading.ledger import LEDGER

    successes = 0
    for i in range(50):
        LEDGER.close_and_journal(
            symbol="AAPL", exit_price=Decimal(100), exit_reasons=["stress_reset"]
        )
        desk.backend.holdings.pop("AAPL", None)
        buy_before = len([m for m in desk.backend.placed if m.side == "buy"])
        opp = desk.offer("AAPL")
        results = _in_parallel(
            [lambda oid=opp.id: desk.approve(oid), lambda oid=opp.id: desk.approve(oid)]
        )
        buy_after = len([m for m in desk.backend.placed if m.side == "buy"])
        delta = buy_after - buy_before
        assert delta <= 1, f"round {i}: +{delta} buys (race)"
        if any(r.status_code == 200 for r in results):
            assert delta == 1, f"round {i}: HTTP 200 without a buy"
            successes += 1
    assert successes >= 1, "expected at least one successful dual-approval round"
