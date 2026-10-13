"""Slow entry admission cannot stall passive coverage or manufacture freshness."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from strategy.orb import retest_data, runtime
from strategy.orb.observation_status import observation_status
from strategy.orb.store import (
    create_session,
    read_runtime_session,
    read_session,
    read_symbol_session,
    update_state,
)
from tests.unit.test_orb_retest import scenario


def test_projected_reads_are_detached_and_preserve_authoritative_claim():
    plan, _, _now = scenario()
    payload = {
        "session": plan.session,
        "plans": {"AAPL": plan.model_dump(mode="json"), "MSFT": {}},
        "states": {"AAPL": {"state": "EXECUTED", "opportunity_id": "held"}},
        "discovery_pool": {"private-history": [1, 2, 3]},
        "rejections": {"X": ["R"]},
    }
    create_session(plan.session, payload)
    projection = read_runtime_session(plan.session)
    assert "discovery_pool" not in projection and "rejections" not in projection
    symbol = read_symbol_session(plan.session, "AAPL")
    assert set(symbol["plans"]) == {"AAPL"}
    assert symbol["states"]["AAPL"]["opportunity_id"] == "held"
    symbol["states"]["AAPL"]["state"] = "WAIT"
    assert read_session(plan.session) == payload
    assert read_symbol_session(plan.session, "MISSING") is None
    assert read_runtime_session("1900-01-01") is None


@pytest.mark.asyncio
async def test_slow_entry_worker_does_not_stall_full_pass_or_advance_queued_freshness(monkeypatch):
    for name in ("_cache", "_failures", "_coverage", "_last_attempt", "_probe_after"):
        monkeypatch.setattr(retest_data, name, {})
    plan, bars, instant = scenario()
    other = plan.model_copy(update={"symbol": "MSFT"})
    passive = [
        b.model_copy(
            update={
                "symbol": "MSFT",
                "close": plan.range_high,
                "open": plan.range_high,
                "high": plan.range_high + 1,
                "low": plan.range_high - 1,
            }
        )
        for b in bars
    ]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz or UTC)

    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr(runtime, "_pending_entries", set())
    monkeypatch.setattr(runtime, "_entry_worker", None)
    monkeypatch.setattr(runtime, "_evaluating", set())
    create_session(
        plan.session,
        {
            "session": plan.session,
            "feed": "sip",
            "plans": {p.symbol: p.model_dump(mode="json") for p in [plan, other]},
        },
    )
    entered, release = asyncio.Event(), asyncio.Event()
    feed = SimpleNamespace(
        get_bars_batch=AsyncMock(return_value={"AAPL": bars, "MSFT": passive}),
        get_snapshots=AsyncMock(return_value={}),
    )
    broker = SimpleNamespace(place_order=AsyncMock())
    ctx = SimpleNamespace(market_data=feed, broker=broker)

    @asynccontextmanager
    async def context(*args):
        yield ctx

    async def slow(symbol, ctx):
        entered.set()
        await release.wait()
        update_state(plan.session, symbol, {"state": "WAIT", "observed_at": instant.isoformat()})
        return SimpleNamespace(status="wait_for_entry")

    monkeypatch.setattr(runtime, "open_scan_context", context)
    monkeypatch.setattr(runtime, "evaluate_symbol", slow)
    runtime.start_entry_evaluator()
    task = runtime._entry_worker
    try:
        await asyncio.wait_for(runtime._observe_once(ctx), 1)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(runtime._observe_once(ctx), 1)
        stored = read_session(plan.session)
        assert observation_status(stored, now=instant)["checked_recently"] == 1
        assert "last_checked_at" not in stored.get("states", {}).get("AAPL", {})
        assert runtime._pending_entries == {"AAPL"}
        release.set()
        for _ in range(100):
            if not runtime._pending_entries:
                break
            await asyncio.sleep(0.01)
        assert not runtime._pending_entries
        assert observation_status(read_session(plan.session), now=instant)["checked_recently"] == 2
        broker.place_order.assert_not_awaited()
    finally:
        release.set()
        runtime.stop_entry_evaluator()
        await asyncio.gather(task, return_exceptions=True)
