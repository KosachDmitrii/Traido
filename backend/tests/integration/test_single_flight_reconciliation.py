"""P0-4: two overlapping requests must produce one reconciliation pass.

The audit found that `GET /api/v1/desk/broker` mutates the broker and that
`?fresh=true` skips every throttle. That is reachable by accident — a browser
prefetch, a retried poll, two dashboard tabs — and each trigger runs a full pass
that installs, resizes and cancels orders.

Counting passes rather than mutations is the point here. Mutations are what
hurts, but a mutation only appears when there is also something to fix, so a
test that watched mutations would pass on a healthy book and prove nothing.
"""

from __future__ import annotations

from tests.integration.test_reconciliation_and_races_end_to_end import (
    _in_parallel,
    hold_both_readers_at_the_order_book,
)


def _passes() -> int:
    from trading.reconcile_supervisor import RECONCILE

    return RECONCILE.status.passes


def test_two_overlapping_fresh_requests_run_one_pass(desk, monkeypatch) -> None:
    before = _passes()
    hold_both_readers_at_the_order_book(desk, monkeypatch)

    responses = _in_parallel([desk.refresh_broker, desk.refresh_broker])

    assert all(r.status_code == 200 for r in responses)
    assert _passes() - before == 1, (
        "two callers asked for fresh broker truth at the same moment; they must "
        f"share one pass, not start two — ran {_passes() - before}"
    )


def test_both_callers_still_get_an_answer(desk, monkeypatch) -> None:
    """Coalescing must not mean the second caller is turned away.

    It asked for fresh data and it gets fresh data — the result of the pass that
    was already running, which is as fresh as anything it could have started.
    """
    hold_both_readers_at_the_order_book(desk, monkeypatch)

    responses = _in_parallel([desk.refresh_broker, desk.refresh_broker])

    for r in responses:
        body = r.json()
        assert body["reconciliation"]["ok"] is True, body["reconciliation"]
        assert body["reconciliation"]["last_success_at"] is not None


def test_a_sequential_second_request_inside_the_interval_does_not_rerun(desk) -> None:
    """Without `fresh`, the interval still applies."""
    desk.refresh_broker(fresh=True)
    after_first = _passes()

    desk.refresh_broker(fresh=False)

    assert _passes() == after_first, "a throttled request must not run a pass"


def test_a_failing_pass_is_recorded_rather_than_raised(desk, monkeypatch) -> None:
    """The desk must still answer, and must say that it could not check."""
    from trading.reconcile_supervisor import RECONCILE

    async def _explode() -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(desk.broker, "list_positions", _explode)
    RECONCILE.reset()

    response = desk.refresh_broker()

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["ok"] is False
    assert "broker unreachable" in (reconciliation["error"] or "")
    assert reconciliation["last_success_at"] is None, (
        "a failed pass must not advance the freshness clock"
    )
