"""Bounded work must preserve coverage, corrections, claims and freshness."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from database.session import get_sync_engine
from market_data.bar_store import load, save_many
from strategy.orb import runtime
from strategy.orb.observation_status import observation_status
from strategy.orb.store import create_session, read_session, update_state
from tests.unit.test_orb_retest import scenario


def test_bulk_upsert_reduces_sql_and_last_correction_wins():
    engine = get_sync_engine()
    inserts = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT"):
            inserts.append(statement)

    start = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
    rows = [(start + timedelta(minutes=i), {"close": str(i)}) for i in range(250)]
    rows.append((start, {"close": "corrected"}))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        save_many("alpaca:sip", "1Min", {"AAPL": rows})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(inserts) == 3
    stored = load("alpaca:sip", "AAPL", "1Min", start, start + timedelta(days=1))
    assert len(stored) == 250
    assert stored[0]["close"] == "corrected"
    assert load("alpaca:other", "AAPL", "1Min", start, start + timedelta(days=1)) == []


def test_oldest_first_schedule_covers_tail_and_survives_reload():
    now = datetime.now(UTC)
    plans = {f"S{i:04}": {} for i in range(257)}
    session = {"plans": plans, "states": {}}
    seen = set()
    for _ in range(3):
        batch = runtime.observation_order(session, plans)[: runtime.OBSERVATION_BATCH]
        seen.update(batch)
        for symbol in batch:
            session["states"][symbol] = {"observed_at": now.isoformat()}
        session = json.loads(json.dumps(session))
    assert seen == set(plans)


def test_older_observation_cannot_overwrite_newer_state_or_claim():
    plan, _, now = scenario()
    create_session(plan.session, {"plans": {plan.symbol: plan.model_dump(mode="json")}})
    update_state(
        plan.session,
        plan.symbol,
        {
            "state": "DATA_BLOCKED",
            "reasons": ["ORB_QUOTE_STALE"],
            "observed_at": now.isoformat(),
            "opportunity_id": "existing-claim",
        },
    )
    update_state(
        plan.session,
        plan.symbol,
        {
            "state": "WAIT",
            "observed_at": (now - timedelta(seconds=1)).isoformat(),
        },
    )
    state = read_session(plan.session)["states"][plan.symbol]
    assert state["state"] == "DATA_BLOCKED"
    assert state["opportunity_id"] == "existing-claim"


def test_observation_progress_does_not_equate_checked_with_valid_data():
    now = datetime.now(UTC)
    session = {
        "plans": {s: {} for s in ["A", "B", "C"]},
        "states": {
            "A": {
                "state": "DATA_BLOCKED",
                "observed_at": now.isoformat(),
                "reasons": ["ORB_QUOTE_STALE"],
            },
            "B": {"state": "WAIT", "observed_at": (now - timedelta(minutes=6)).isoformat()},
        },
    }
    status = observation_status(session, now=now)
    assert status["checked_recently"] == 1
    assert status["pending"] == 2
    assert status["blocked_reasons"] == {"ORB_QUOTE_STALE": 1}


@pytest.mark.asyncio
async def test_one_symbol_failure_does_not_abort_batch_and_places_no_order(monkeypatch):
    plan, _, _ = scenario()
    other = plan.model_copy(update={"symbol": "MSFT"})
    create_session(
        plan.session, {"plans": {p.symbol: p.model_dump(mode="json") for p in [plan, other]}}
    )
    broker = SimpleNamespace(place_order=AsyncMock())

    async def evaluate(symbol, ctx):
        if symbol == plan.symbol:
            raise TimeoutError
        return SimpleNamespace(status="wait_for_entry")

    monkeypatch.setattr(runtime, "evaluate_symbol", evaluate)
    counts = await runtime.evaluate_observation_batch(
        {"session": plan.session}, [plan.symbol, other.symbol], SimpleNamespace(broker=broker)
    )
    assert counts == {"data_blocked": 1, "wait_for_entry": 1}
    assert read_session(plan.session)["states"][plan.symbol]["reasons"] == [
        "ORB_OBSERVATION_TIMEOUT"
    ]
    broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_observation_does_not_evaluate_same_symbol_twice(monkeypatch):
    plan, _, _ = scenario()
    create_session(plan.session, {"plans": {plan.symbol: plan.model_dump(mode="json")}})
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def evaluate(symbol, ctx):
        calls.append(symbol)
        started.set()
        await release.wait()
        return SimpleNamespace(status="wait_for_entry")

    monkeypatch.setattr(runtime, "evaluate_symbol", evaluate)
    monkeypatch.setattr(runtime, "_evaluating", set())
    stored = {"session": plan.session}
    first = asyncio.create_task(runtime.evaluate_observation_batch(stored, [plan.symbol], None))
    await started.wait()
    try:
        assert await runtime.evaluate_observation_batch(stored, [plan.symbol], None) == {}
    finally:
        release.set()
        await first
    assert calls == [plan.symbol]


@pytest.mark.asyncio
async def test_claimed_symbol_advances_queue_without_renewing_market_evidence(monkeypatch):
    plan, _, now = scenario()
    old = (now - timedelta(minutes=10)).isoformat()
    create_session(
        plan.session,
        {
            "plans": {plan.symbol: plan.model_dump(mode="json"), "MSFT": {}},
            "states": {
                plan.symbol: {
                    "state": "EXECUTED",
                    "opportunity_id": "claim",
                    "observed_at": old,
                    "quote_at": old,
                },
                "MSFT": {"observed_at": old},
            },
        },
    )
    monkeypatch.setattr(
        runtime, "evaluate_symbol", AsyncMock(return_value=SimpleNamespace(status="executed"))
    )
    await runtime.evaluate_observation_batch({"session": plan.session}, [plan.symbol], None)
    stored = read_session(plan.session)
    state = stored["states"][plan.symbol]
    assert state["last_checked_at"] > old
    assert state["state"] == "EXECUTED"
    assert state["opportunity_id"] == "claim"
    assert state["observed_at"] == state["quote_at"] == old
    assert runtime.observation_order(stored, stored["plans"])[0] == "MSFT"


@pytest.mark.asyncio
async def test_busy_stream_subscribes_new_symbols_without_idle_timeout(monkeypatch):
    from market_data import alpaca_stream
    from strategy.orb import store

    sent = []
    clock = [0.0]
    symbols = ["AAPL"]
    frames = iter(
        [
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "bars": ["AAPL"], "updatedBars": ["AAPL"]}],
            [{"T": "subscription", "bars": ["AAPL", "GOOGL"], "updatedBars": ["AAPL", "GOOGL"]}],
        ]
    )

    class Socket:
        async def send(self, value):
            sent.append(json.loads(value))

        async def recv(self):
            try:
                frame = next(frames)
            except StopIteration:
                raise asyncio.CancelledError from None
            if frame[0]["T"] == "subscription":
                clock[0] = 31.0
                symbols.append("GOOGL")
            return json.dumps(frame)

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    import websockets.asyncio.client

    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **kw: Connection())
    monkeypatch.setattr(alpaca_stream.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store, "read_session", lambda day: {"plans": {s: {} for s in symbols}})
    with pytest.raises(asyncio.CancelledError):
        await alpaca_stream._run("test-key", "test-secret")
    subscriptions = [m for m in sent if m["action"] == "subscribe"]
    assert subscriptions[0]["bars"] == ["AAPL"]
    assert subscriptions[1]["bars"] == ["GOOGL"]
    assert subscriptions[1]["updatedBars"] == ["GOOGL"]


@pytest.mark.asyncio
async def test_real_observation_pass_limits_history_and_eventually_checks_entire_universe(
    monkeypatch,
):
    from strategy.orb import retest, retest_data

    plan, _, instant = scenario()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz or UTC)

    monkeypatch.setattr(runtime, "datetime", Clock)
    plans = {
        f"S{i:04}": plan.model_copy(update={"symbol": f"S{i:04}"}).model_dump(mode="json")
        for i in range(1401)
    }
    create_session(plan.session, {"session": plan.session, "feed": "sip", "plans": plans})
    primed = []

    async def prime(provider, selected, *, now):
        primed.append([p.symbol for p in selected])

    async def histories(selected, *, now):
        return {p.symbol: [] for p in selected}, {}

    monkeypatch.setattr(retest_data, "prime_bars", prime)
    monkeypatch.setattr(retest_data, "read_cached_bars", histories)
    monkeypatch.setattr(retest_data, "coverage_end", lambda p: instant)
    monkeypatch.setattr(
        retest,
        "rebuild",
        lambda *a, **kw: SimpleNamespace(
            state="WAIT", reasons=["ORB_RETEST_WAIT_BREAKOUT"], plan=None
        ),
    )
    evaluate = AsyncMock()
    monkeypatch.setattr(runtime, "evaluate_symbol", evaluate)
    broker = SimpleNamespace(place_order=AsyncMock())
    context = SimpleNamespace(market_data=SimpleNamespace(), broker=broker)
    for _ in range(8):
        result = await runtime._observe_once(context)
        assert result == {"wait_for_entry": 200}
        instant += timedelta(seconds=1)
    assert all(len(batch) == 200 for batch in primed)
    assert set().union(*map(set, primed)) == set(plans)
    assert runtime.OBSERVATION["pending"] == 0
    evaluate.assert_not_awaited()
    broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_priority_recovery_failure_retains_deferred_events_and_newer_correction(monkeypatch):
    from strategy.orb import retest_data

    plan, bars, instant = scenario()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz or UTC)

    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr(runtime, "_pending_completed_bars", {})
    plans = {
        f"S{i:02}": plan.model_copy(update={"symbol": f"S{i:02}"}).model_dump(mode="json")
        for i in range(10)
    }
    create_session(plan.session, {"session": plan.session, "plans": plans})
    for symbol in plans:
        runtime.notify_completed_bar(bars[0].model_copy(update={"symbol": symbol}))
    original = runtime._pending_completed_bars.copy()
    correction = bars[0].model_copy(update={"symbol": "S00", "volume": bars[0].volume + 1})

    async def prime(provider, selected, *, now):
        assert len(selected) == runtime.ENTRY_BATCH
        runtime.notify_completed_bar(correction)
        raise TimeoutError

    monkeypatch.setattr(retest_data, "history_complete", lambda *a, **kw: False)
    monkeypatch.setattr(retest_data, "prime_bars", prime)
    with pytest.raises(TimeoutError):
        await runtime.observe_priority(SimpleNamespace(market_data=SimpleNamespace()))
    assert set(runtime._pending_completed_bars) == set(original)
    assert runtime._pending_completed_bars[("S00", correction.ts)] == correction
