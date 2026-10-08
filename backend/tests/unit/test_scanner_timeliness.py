"""Discovery progress, durable gap recovery and source-time diagnostics."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.scanner import agent as scanner
from strategy.orb import intraday, runtime
from strategy.orb.store import merge_intraday_discovery, read_session
from tests.unit.test_orb_policy import NOW
from tests.unit.test_orb_session import Context, Universe


def test_pending_windows_prioritize_current_then_oldest_gap():
    latest = NOW.astimezone(intraday.ET)
    first = latest.replace(hour=9, minute=40)
    saved = {"intraday_completed_ranges": [first.isoformat()]}
    pending = intraday.pending_windows(saved, latest)
    assert pending[0] == latest
    assert pending[1] == first + timedelta(minutes=5)
    assert all(end <= latest for end in pending)
    assert first not in pending


def test_pending_windows_stop_at_early_close():
    from datetime import datetime

    latest = datetime(2026, 11, 27, 13, 5, tzinfo=intraday.ET)
    pending = intraday.pending_windows({}, latest)
    assert pending
    assert max(pending).hour == 12
    assert max(pending).minute == 55


@pytest.mark.asyncio
async def test_success_is_durable_and_failed_window_is_not_completed():
    ctx = Context(["AAPL"])
    saved = await runtime.discover(ctx, Universe(["AAPL"]), now=NOW)
    latest = NOW.astimezone(intraday.ET)
    diagnostics = {
        "status": "ready",
        "range_end": latest.isoformat(),
        "evaluated_at": NOW.isoformat(),
    }
    merge_intraday_discovery(saved["session"], diagnostics=diagnostics)
    # Reloading authoritative state models a process restart, not a cache hit.
    saved = read_session(saved["session"])
    assert latest not in intraday.pending_windows(saved, latest)
    gap = intraday.pending_windows(saved, latest)[0]
    diagnostics.update(status="data_blocked", range_end=gap.isoformat())
    merge_intraday_discovery(saved["session"], diagnostics=diagnostics)
    saved = read_session(saved["session"])
    assert gap in intraday.pending_windows(saved, latest)
    assert latest not in intraday.pending_windows(saved, latest)
    diagnostics["status"] = "ready"
    merge_intraday_discovery(saved["session"], diagnostics=diagnostics)
    saved = read_session(saved["session"])
    assert gap not in intraday.pending_windows(saved, latest)


@pytest.mark.asyncio
async def test_refresh_recovers_one_gap_without_replacing_claim():
    from strategy.orb.store import update_state

    ctx = Context(["AAPL"])
    original = await runtime.discover(ctx, Universe(["AAPL"]), now=NOW)
    update_state(original["session"], "AAPL", {"state": "EXECUTED", "opportunity_id": "held"})
    first = await intraday.refresh(ctx, Universe([]), now=NOW)
    completed = first["intraday_completed_ranges"]
    second = await intraday.refresh(ctx, Universe([]), now=NOW)
    assert len(second["intraday_completed_ranges"]) == len(completed) + 1
    assert second["intraday_discovery"]["range_end"] < first["intraday_discovery"]["range_end"]
    assert second["plans"] == original["plans"]
    assert second["states"]["AAPL"]["opportunity_id"] == "held"


@pytest.mark.asyncio
async def test_large_universe_is_read_in_bounded_batches(monkeypatch):
    from copy import deepcopy

    from database.models.orb import OrbSessionRow
    from database.session import session_factory

    ctx = Context(["AAPL"])
    original = await runtime.discover(ctx, Universe(["AAPL"]), now=NOW)
    with session_factory()() as db:
        row = db.get(OrbSessionRow, original["session"])
        payload = deepcopy(row.payload)
        baseline = payload["discovery_pool"]["AAPL"]
        payload["discovery_pool"].update({f"TEST{i}": baseline for i in range(201)})
        row.payload = payload
        db.commit()
    calls = []

    async def empty_window(symbols, start, end, timeframe):
        calls.append(list(symbols))
        return {}

    monkeypatch.setattr(ctx.market_data, "get_bars_batch", empty_window)
    result = await intraday.refresh(ctx, Universe([]), now=NOW)
    assert [len(batch) for batch in calls] == [100, 100, 1]
    assert result["intraday_discovery"]["status"] == "ready"
    assert result["plans"] == original["plans"]
    assert result["intraday_discovery"]["added"] == []


@pytest.mark.asyncio
async def test_failed_history_read_drains_siblings_before_retry(monkeypatch):
    from copy import deepcopy

    from database.models.orb import OrbSessionRow
    from database.session import session_factory
    from tests.unit.test_orb_intraday import window_evidence

    ctx = Context(["AAPL"])
    original = await runtime.discover(ctx, Universe(["AAPL"]), now=NOW)
    with session_factory()() as db:
        row = db.get(OrbSessionRow, original["session"])
        payload = deepcopy(row.payload)
        payload["discovery_pool"]["GOOGL"] = payload["discovery_pool"]["AAPL"]
        row.payload = payload
        db.commit()
    _, windows = window_evidence()
    current = windows[-1].model_copy(update={"symbol": "GOOGL"})
    sibling_started = asyncio.Event()
    active = 0
    history_calls = 0

    async def history(symbols, start, end, timeframe):
        nonlocal active, history_calls
        if start.date() == current.ts.date():
            return {"GOOGL": [current]}
        history_calls += 1
        number = history_calls
        active += 1
        try:
            if number == 1:
                await sibling_started.wait()
                raise TimeoutError("test history outage")
            sibling_started.set()
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(ctx.market_data, "get_bars_batch", history)
    result = await asyncio.wait_for(intraday.refresh(ctx, Universe([]), now=NOW), 1)
    assert result["intraday_discovery"]["status"] == "data_blocked"
    assert active == 0
    assert result["intraday_completed_ranges"] == []
    assert result["plans"] == original["plans"]


@pytest.mark.asyncio
async def test_discovery_runs_while_full_observation_is_waiting(monkeypatch):
    from agents.scanner import cycle
    from market_data import sector_preflight
    from risk import kill_switch
    from trading import scan_context

    entered = asyncio.Event()
    independently_refreshed = asyncio.Event()
    ctx = SimpleNamespace(scan_id=uuid4())
    calls = []

    async def discover(*args):
        return {"status": "ready", "plans": {}}

    async def refresh(*args):
        calls.append(True)
        independently_refreshed.set()
        return {"status": "ready", "plans": {}}

    async def observe(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def context(*args):
        yield ctx

    monkeypatch.setattr(runtime, "discover", discover)
    monkeypatch.setattr(runtime, "observe", observe)
    monkeypatch.setattr(runtime, "retire_pending_legacy", lambda: None)
    monkeypatch.setattr(intraday, "refresh", refresh)
    monkeypatch.setattr(intraday, "us_equity_rth_open", lambda now: True)
    monkeypatch.setattr(scanner, "load_watchlist", lambda: {"enabled": True})
    monkeypatch.setattr(scanner, "universe_service", lambda: None)
    monkeypatch.setattr(kill_switch, "is_kill_switch_on", lambda: False)
    monkeypatch.setattr(scan_context, "open_scan_context", context)
    monkeypatch.setattr(sector_preflight, "offer", lambda data: None)
    scan = asyncio.create_task(cycle.run_cycle(universe_service=None, context=ctx))
    discovery = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        discovery = asyncio.create_task(intraday.discovery_loop())
        await asyncio.wait_for(independently_refreshed.wait(), 1)
        assert len(calls) == 1
        assert not scan.done()
    finally:
        scan.cancel()
        if discovery:
            discovery.cancel()
        await asyncio.gather(scan, *([discovery] if discovery else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("provided_context", [False, True])
async def test_full_scanner_does_not_wait_for_stalled_rolling_discovery(
    monkeypatch, provided_context
):
    from agents.scanner import cycle
    from market_data import sector_preflight
    from strategy.orb import store

    started = asyncio.Event()
    checked = asyncio.Event()
    saved = {"status": "ready", "session": "2026-10-08", "plans": {}}
    ctx = SimpleNamespace(scan_id=uuid4())

    async def refresh(*args):
        started.set()
        await asyncio.Event().wait()

    async def observe(**kwargs):
        checked.set()
        return {"wait_for_entry": 1}

    @asynccontextmanager
    async def context(*args):
        yield ctx

    monkeypatch.setattr(runtime, "discover", AsyncMock(return_value=saved))
    monkeypatch.setattr(runtime, "observe", observe)
    monkeypatch.setattr(runtime, "retire_pending_legacy", lambda: None)
    monkeypatch.setattr(intraday, "refresh", refresh)
    monkeypatch.setattr(cycle, "open_scan_context", context)
    monkeypatch.setattr(sector_preflight, "offer", lambda data: None)
    monkeypatch.setattr(store, "read_session", lambda day: saved)
    discovery = asyncio.create_task(intraday.refresh(ctx, None))
    try:
        await asyncio.wait_for(started.wait(), 1)
        result = await asyncio.wait_for(
            cycle.run_cycle(universe_service=None, context=ctx if provided_context else None), 1
        )
        assert checked.is_set()
        assert not discovery.done()
        assert result.funnel.wait_for_entry == 1
    finally:
        discovery.cancel()
        await asyncio.gather(discovery, return_exceptions=True)


@pytest.mark.asyncio
async def test_shielded_observation_expires_and_next_pass_can_restart(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime, "_observation_task", None)
    monkeypatch.setattr(runtime, "_observe_once", stuck)
    monkeypatch.setattr(
        runtime, "get_settings", lambda: SimpleNamespace(scanner_cycle_timeout_seconds=0.03)
    )
    caller = asyncio.create_task(runtime.observe())
    await entered.wait()
    shared = runtime._observation_task
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with pytest.raises(TimeoutError):
        await shared
    assert cancelled.is_set()

    async def recovered(context):
        return {"wait_for_entry": 1}

    monkeypatch.setattr(runtime, "_observe_once", recovered)
    assert await runtime.observe() == {"wait_for_entry": 1}
    assert runtime._observation_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("age,healthy", [(299, True), (301, False)])
async def test_health_checks_progress_age(monkeypatch, age, healthy):
    monkeypatch.setattr(scanner, "_task", asyncio.current_task())
    monkeypatch.setattr(scanner, "_supervisor_error", None)
    monkeypatch.setattr(scanner, "_supervisor_heartbeat", time.monotonic() - age)
    monkeypatch.setattr(scanner.STATUS, "error", None)
    monkeypatch.setattr(scanner, "load_watchlist", lambda: {"scan_interval_seconds": 90})
    monkeypatch.setattr(intraday, "_heartbeat", None)
    monkeypatch.setattr(intraday, "_task", None)
    assert scanner.scanner_health()[0] is healthy


@pytest.mark.asyncio
async def test_quote_diagnostics_preserve_source_timestamp(monkeypatch, caplog):
    from datetime import UTC, datetime

    from market_data.providers.alpaca import AlpacaMarketData

    feed = AlpacaMarketData(
        api_key="test-key",
        api_secret="test-secret",
        base_url="https://data.alpaca.markets",
        feed="sip",
    )
    source = datetime.now(UTC) - timedelta(seconds=10)

    async def payload(*args, **kwargs):
        return {"quote": {"bp": 100, "ap": 101, "t": source.isoformat()}}

    monkeypatch.setattr(feed, "_get_json", payload)
    with caplog.at_level("INFO"):
        result = await feed.get_quote("AAPL")
    assert result.ts == source
    assert "age_seconds=" in caplog.text
    assert "request_seconds=" in caplog.text
    assert "test-key" not in caplog.text
    assert "test-secret" not in caplog.text
