"""P0-2: broker truth is re-read on a timer, not when a browser asks.

The audit's finding was structural rather than subtle. `api/main.py` started
exactly one background task — the scanner — and reconciliation lived inside the
handler for `GET /api/v1/desk/broker`. So the answer to "is this desk checking
itself against the broker" was "only while a dashboard tab is open", and the
hours when nobody has one open are exactly the hours a position sits unattended.

These tests issue no HTTP requests at all. That is the point.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from trading.reconcile_supervisor import (
    RECONCILE,
    ReconciliationSupervisor,
    reconcile_loop_running,
    start_reconcile_loop,
    stop_reconcile_loop,
)


@pytest.fixture(autouse=True)
def stop_any_loop():
    yield
    stop_reconcile_loop()


async def test_the_loop_runs_passes_without_any_request() -> None:
    ran = 0

    async def _pass() -> None:
        nonlocal ran
        ran += 1

    RECONCILE.reset()
    start_reconcile_loop(_pass, interval_sec=0.01)
    await asyncio.sleep(0.1)

    assert ran >= 2, f"the loop should have ticked repeatedly, ran {ran}"
    assert RECONCILE.status.ok is True


async def test_a_failing_pass_does_not_kill_the_loop() -> None:
    """A supervisor that stops after one bad tick is worse than none.

    The desk would keep answering 200 with numbers that quietly stopped being
    checked, which is indistinguishable from a healthy desk.
    """
    calls = 0

    async def _flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("broker unreachable")

    RECONCILE.reset()
    start_reconcile_loop(_flaky, interval_sec=0.01)
    await asyncio.sleep(0.1)

    assert calls >= 2, "the loop must survive a failed pass"
    assert reconcile_loop_running()
    assert RECONCILE.status.ok is True, "and must recover once the broker answers"


async def test_a_factory_that_cannot_build_a_broker_does_not_kill_the_loop() -> None:
    """The one failure the supervisor cannot absorb, because it happens before the pass."""
    calls = 0

    def _factory():
        nonlocal calls
        calls += 1
        raise RuntimeError("no broker credentials")

    start_reconcile_loop(_factory, interval_sec=0.01)
    await asyncio.sleep(0.1)

    assert calls >= 2
    assert reconcile_loop_running()


async def test_starting_twice_does_not_create_two_loops() -> None:
    """Two loops would reinstate the very race single-flight was added to close."""
    ran = 0

    async def _pass() -> None:
        nonlocal ran
        ran += 1
        await asyncio.sleep(0.05)

    RECONCILE.reset()
    start_reconcile_loop(_pass, interval_sec=0.01)
    start_reconcile_loop(_pass, interval_sec=0.01)
    await asyncio.sleep(0.12)

    assert RECONCILE.status.passes == ran, "every pass must have gone through the supervisor"


async def test_the_loop_and_a_caller_share_one_pass() -> None:
    """The ordering constraint from the gap register, asserted rather than trusted.

    A timer plus an unguarded trigger is worse than the trigger alone: it makes
    the duplicate-stop race routine instead of occasional. The loop must go
    through the same supervisor a request does.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    ran = 0

    async def _slow() -> None:
        nonlocal ran
        ran += 1
        started.set()
        await release.wait()

    RECONCILE.reset()
    start_reconcile_loop(_slow, interval_sec=0.01)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    joiner = asyncio.create_task(RECONCILE.run(_slow))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(joiner, timeout=2.0)

    assert ran == 1, f"the joining caller must not have started a second pass, ran {ran}"


async def test_the_supervisor_reports_never_checked_before_the_first_pass() -> None:
    """`None` is not a large number, and a gate on freshness has to tell them apart."""
    fresh = ReconciliationSupervisor()

    assert fresh.age_seconds() is None
    assert fresh.status.has_succeeded is False
    assert fresh.status.as_dict()["stale_seconds"] is None


async def test_age_advances_only_on_success() -> None:
    supervisor = ReconciliationSupervisor()

    async def _fail() -> None:
        raise RuntimeError("nope")

    await supervisor.run(_fail)
    assert supervisor.age_seconds() is None, "a failed pass is not a check"

    async def _ok() -> None:
        return None

    await supervisor.run(_ok)
    age = supervisor.age_seconds()
    assert age is not None and age < 1.0


async def test_the_lifespan_wires_the_real_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The composition root actually starts it — the defect was never in the loop."""
    import api.main as main_mod

    started: list[object] = []
    monkeypatch.setattr(main_mod, "start_scanner", lambda: None)
    monkeypatch.setattr(main_mod, "stop_scanner", lambda: None)
    monkeypatch.setattr(main_mod, "start_reconcile_loop", lambda factory: started.append(factory))
    monkeypatch.setattr(main_mod, "stop_reconcile_loop", lambda: None)

    async with main_mod.lifespan(main_mod.app):
        pass

    assert started == [main_mod.build_reconcile_pass]


def test_the_pass_factory_builds_a_fully_armed_service(desk) -> None:
    """The loop must not be a second, weaker wiring of the same job."""
    from api.deps import build_execution_service, build_reconcile_pass

    service = build_execution_service()
    assert service.market_data is not None

    coro = build_reconcile_pass()
    coro.close()


def test_the_loop_protects_a_position_nobody_is_watching(desk) -> None:
    """End to end, with the dashboard closed.

    The position is stranded at the venue and then a pass is driven directly
    through the supervisor — the same call the timer makes — with no request in
    between.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.strand_position()
    assert desk.resting_protection("AAPL") == []

    from api.deps import build_reconcile_pass

    desk.run_in_app_loop(RECONCILE.run(build_reconcile_pass))

    resting = desk.resting_protection("AAPL")
    assert len(resting) == 1, f"the loop must re-protect it: {resting}"
    assert Decimal(resting[0]["qty"]) == Decimal(str(desk.venue_holdings("AAPL")))
