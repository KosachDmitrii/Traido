"""Scanner ownership and cancellation apply equally to ORB discovery."""

import asyncio

import pytest

from agents.scanner import agent as scanner
from agents.scanner.cycle import CycleResult
from tests.scanner_fakes import scanner_settings, universe_service_for


@pytest.fixture(autouse=True)
def scanner_state(monkeypatch):
    monkeypatch.setattr(scanner, "get_settings", lambda: scanner_settings())
    monkeypatch.setattr(scanner, "load_watchlist", lambda: {"universe": ["AAA"], "enabled": True})
    monkeypatch.setattr(scanner, "universe_service", lambda _: universe_service_for(["AAA"]))
    monkeypatch.setattr(scanner.STATUS, "cycle", 0)
    monkeypatch.setattr(scanner.STATUS, "enabled", True)
    monkeypatch.setattr(scanner.BOARD, "log", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_cycle(monkeypatch):
    calls = []

    async def cycle(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.01)
        return CycleResult()

    monkeypatch.setattr(scanner, "run_cycle", cycle)
    await asyncio.gather(*(scanner.run_scan_cycle() for _ in range(4)))
    assert len(calls) == 1
    assert scanner.STATUS.cycle == 1


@pytest.mark.asyncio
async def test_failed_cycle_releases_ownership(monkeypatch):
    async def fail(**kwargs):
        raise RuntimeError("vendor failed")

    monkeypatch.setattr(scanner, "run_cycle", fail)
    await scanner.run_scan_cycle()
    calls = []

    async def ok(**kwargs):
        calls.append(1)
        return CycleResult()

    monkeypatch.setattr(scanner, "run_cycle", ok)
    await scanner.run_scan_cycle()
    assert calls == [1]


@pytest.mark.asyncio
async def test_cancelling_discovery_does_not_leave_a_second_worker(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(scanner, "run_cycle", slow)
    task = asyncio.create_task(scanner.run_scan_cycle())
    await asyncio.wait_for(started.wait(), 1)
    assert scanner.abort_scan_cycle()
    result = await asyncio.wait_for(task, 1)
    assert result.error == "superseded"
    assert stopped.is_set() and not scanner.STATUS.running
