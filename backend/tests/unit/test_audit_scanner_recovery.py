"""The supervisor retries failures but never swallows shutdown cancellation."""

import asyncio
from types import SimpleNamespace

import pytest

from agents.scanner import agent as scanner


@pytest.mark.asyncio
async def test_supervisor_recovers_and_propagates_cancel(monkeypatch):
    calls = []

    async def inner():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("simulated database outage")
        raise asyncio.CancelledError

    monkeypatch.setattr(scanner, "_scanner_loop_inner", inner)
    monkeypatch.setattr(scanner, "PAUSED_RETRY_SECONDS", 0)
    monkeypatch.setattr(scanner, "_supervisor_error", None)
    monkeypatch.setattr(scanner, "_schedule", None)
    with pytest.raises(asyncio.CancelledError):
        await scanner.scanner_loop()
    assert len(calls) == 2
    assert scanner._supervisor_error == "scanner recovering after RuntimeError"


def test_health_reports_missing_task(monkeypatch):
    monkeypatch.setattr(scanner, "_task", None)
    ok, detail = scanner.scanner_health()
    assert not ok
    assert "not running" in detail


@pytest.mark.asyncio
async def test_health_reports_recovery(monkeypatch):
    monkeypatch.setattr(scanner, "_task", asyncio.current_task())
    monkeypatch.setattr(scanner, "_supervisor_error", "scanner recovering after RuntimeError")
    ok, detail = scanner.scanner_health()
    assert not ok
    assert "recovering" in detail


@pytest.mark.asyncio
async def test_timed_out_cycle_is_cancelled_before_retry(monkeypatch):
    active = 0
    calls = 0
    cancelled = []

    async def cycle():
        nonlocal active, calls
        assert active == 0
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            cancelled.append(True)

    monkeypatch.setattr(scanner, "run_scan_cycle", cycle)
    monkeypatch.setattr(scanner, "load_watchlist", lambda: {"scan_interval_seconds": 90})
    monkeypatch.setattr(
        scanner, "get_settings", lambda: SimpleNamespace(scanner_cycle_timeout_seconds=0.01)
    )
    monkeypatch.setattr(scanner, "PAUSED_RETRY_SECONDS", 0)
    monkeypatch.setattr(scanner, "_schedule", None)
    monkeypatch.setattr(scanner, "_supervisor_error", None)
    monkeypatch.setattr(scanner, "_supervisor_heartbeat", None)
    with pytest.raises(asyncio.CancelledError):
        await scanner.scanner_loop()
    assert cancelled == [True]
    assert calls == 2
    assert active == 0
