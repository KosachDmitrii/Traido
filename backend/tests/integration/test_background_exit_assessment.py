"""Open positions are re-judged on a timer, not when a browser asks.

The exit assessment ran inside the handler for `GET /api/v1/desk/broker`, so a
sell proposal was raised the next time somebody looked at the dashboard and —
worse — a proposal whose reason had stopped holding stayed on the board until
then. Withdrawal is a control action; a control action driven by a page render
only happens while someone is watching.

Mirrors `test_background_reconciliation.py`, because the failure modes are the
same ones: a loop that dies of one bad pass, and two loops where there should be
one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from agents.position.loop import (
    position_loop_running,
    start_position_loop,
    stop_position_loop,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_loop_left_running() -> Iterator[None]:
    stop_position_loop()
    yield
    stop_position_loop()


async def test_the_loop_assesses_without_anyone_asking() -> None:
    calls = 0

    async def _pass() -> None:
        nonlocal calls
        calls += 1

    start_position_loop(_pass, interval_sec=0.01)
    await asyncio.sleep(0.1)

    assert calls >= 2
    assert position_loop_running()


async def test_a_failing_pass_does_not_kill_the_loop() -> None:
    """A vendor outage must not end the watch on open positions.

    A supervisor that stops after one bad tick is worse than none: the desk goes
    on looking healthy while nothing is being judged.
    """
    calls = 0

    async def _flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("market data unavailable")

    start_position_loop(_flaky, interval_sec=0.01)
    await asyncio.sleep(0.1)

    assert calls >= 2
    assert position_loop_running()


async def test_a_factory_that_cannot_build_its_vendors_does_not_kill_the_loop() -> None:
    """The failure that happens before the pass, so the pass cannot absorb it."""
    calls = 0

    def _factory():
        nonlocal calls
        calls += 1
        raise RuntimeError("no market data credentials")

    start_position_loop(_factory, interval_sec=0.01)
    await asyncio.sleep(0.1)

    assert calls >= 2
    assert position_loop_running()


async def test_starting_twice_does_not_create_two_loops() -> None:
    """Two loops would raise and withdraw the same card against each other."""
    ran = 0

    async def _pass() -> None:
        nonlocal ran
        ran += 1

    start_position_loop(_pass, interval_sec=0.05)
    start_position_loop(_pass, interval_sec=0.05)
    await asyncio.sleep(0.12)

    assert ran <= 3, "a second loop is doubling the assessment rate"


async def test_stopping_ends_the_loop() -> None:
    calls = 0

    async def _pass() -> None:
        nonlocal calls
        calls += 1

    start_position_loop(_pass, interval_sec=0.01)
    await asyncio.sleep(0.05)
    stop_position_loop()
    settled = calls
    await asyncio.sleep(0.05)

    assert not position_loop_running()
    assert calls == settled
