"""Real durable batches, recovery, correction ordering and bounded async ingestion."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from database.models.desk import OrderIntentRow
from database.session import get_sync_engine, session_factory
from market_data import alpaca_stream as stream
from market_data.bar_store import load_bars, load_bars_many, save_many
from strategy.orb import runtime
from tests.unit.test_durable_market_bars import minute
from tests.unit.test_orb_retest import scenario


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    for name, value in [
        ("_latest", {}),
        ("_completed", {}),
        ("_connected", False),
        ("_connected_at", None),
        ("STREAM_STATUS", {}),
    ]:
        monkeypatch.setattr(stream, name, value)
    monkeypatch.setattr(runtime, "_pending_completed_bars", {})
    monkeypatch.setattr(runtime, "_pending_entries", set())


def test_1400_symbols_use_bulk_transactions_and_emit_only_durable_bars(monkeypatch):
    plan, _, now = scenario()
    symbols = [f"S{i:04}" for i in range(1400)]
    messages = [{**minute(i, plan.range_end), "S": s} for i in range(5) for s in symbols]
    transactions = []
    engine = get_sync_engine()
    capture = transactions.append
    event.listen(engine, "commit", capture)
    assembler = stream.StreamAssembler("sip")
    try:
        for offset in range(0, len(messages), stream.STREAM_BATCH_SIZE):
            assembler.ingest_batch(messages[offset : offset + stream.STREAM_BATCH_SIZE], now=now)
    finally:
        event.remove(engine, "commit", capture)
    bars = load_bars_many("alpaca:sip", symbols, plan.range_end, now)
    assert all(len(values) == 1 and values[0].volume == 50 for values in bars.values())
    assert len(runtime._pending_completed_bars) == 1400
    # 7,000 minutes no longer require 7,000 separate commits/reads.
    assert len(transactions) <= 28
    assert len(assembler.windows) == 1400
    with session_factory()() as db:
        assert db.query(OrderIntentRow).count() == 0


def test_restart_loads_missing_minutes_and_last_correction_wins(monkeypatch):
    plan, _, now = scenario()
    messages = [minute(i, plan.range_end) for i in range(5)]
    save_many(
        "alpaca:sip",
        "1Min",
        {
            plan.symbol: [
                (
                    plan.range_end + timedelta(minutes=i),
                    stream._minute(messages[i], now).model_dump(mode="json"),
                )
                for i in range(4)
            ]
        },
    )
    assembler = stream.StreamAssembler("sip")
    assembler.ingest_batch([messages[4]], now=now)
    assert load_bars(plan.source, plan.symbol, plan.range_end, now)[0].close == 101
    corrections = [{**messages[4], "T": "u", "c": 100}, {**messages[4], "T": "u", "c": 102}]
    assembler.ingest_batch(corrections, now=now)
    assert load_bars(plan.source, plan.symbol, plan.range_end, now)[0].close == 102
    monkeypatch.setattr(stream, "STREAM_WINDOWS", 1)
    assembler.ingest_batch([minute(0, now)], now=now + timedelta(minutes=1))
    assert len(assembler.windows) == 1
    # An evicted historic window is hydrated from durable minutes again.
    assembler.ingest_batch([{**messages[4], "T": "u", "c": 100}], now=now + timedelta(minutes=2))
    assert load_bars(plan.source, plan.symbol, plan.range_end, now)[0].close == 100


def test_failed_completed_bar_commit_cannot_advance_freshness_or_notify(monkeypatch):
    plan, _, now = scenario()
    monkeypatch.setattr(stream, "_connected", True)
    monkeypatch.setattr(stream, "_connected_at", plan.range_end - timedelta(seconds=1))
    monkeypatch.setattr(
        stream, "save_bars_many", lambda *a: (_ for _ in ()).throw(RuntimeError("db offline"))
    )
    with pytest.raises(RuntimeError, match="db offline"):
        stream.StreamAssembler().ingest_batch(
            [minute(i, plan.range_end) for i in range(5)], now=now
        )
    assert not stream._latest and not stream._completed
    assert not runtime._pending_completed_bars
    assert not stream.current(plan.symbol, plan.source, now)


@pytest.mark.asyncio
async def test_receiver_keeps_reading_while_writer_is_blocked_and_shutdown_joins(monkeypatch):
    from websockets.asyncio import client

    from strategy.orb.store import create_session

    now = datetime.now(UTC)
    start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) - timedelta(
        minutes=10
    )
    create_session(str(now.astimezone(stream.ET).date()), {"plans": {"AAPL": {}}})
    entered, release = Event(), Event()
    received = asyncio.Event()
    saved = []

    def slow(self, messages, *, now):
        entered.set()
        assert release.wait(2)
        saved.extend(messages)
        return []

    frames = [
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "bars": ["AAPL"], "updatedBars": ["AAPL"]}],
        [minute(0, start)],
        [minute(1, start)],
    ]

    class Socket:
        async def send(self, value):
            pass

        async def recv(self):
            if frames:
                value = frames.pop(0)
                if len(frames) == 0:
                    await asyncio.to_thread(entered.wait, 1)
                return json.dumps(value)
            received.set()
            await asyncio.Event().wait()

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(client, "connect", lambda *a, **kw: Connection())
    monkeypatch.setattr(stream.StreamAssembler, "ingest_batch", slow)
    task = asyncio.create_task(stream._run("test-key", "test-secret"))
    try:
        await asyncio.wait_for(received.wait(), 1)
        assert entered.is_set() and not saved
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        task.cancel() if not task.cancelling() else None
        await asyncio.gather(task, return_exceptions=True)
    assert [m["t"] for m in saved] == [minute(i, start)["t"] for i in range(2)]
    assert not stream._connected


@pytest.mark.asyncio
async def test_writer_failure_interrupts_a_quiet_receiver():
    async def fail():
        raise RuntimeError("storage failed")

    writer = asyncio.create_task(fail())
    socket = SimpleNamespace(recv=AsyncMock(side_effect=lambda: asyncio.Event().wait()))

    # Use a real pending receiver; AsyncMock must not return a nested coroutine.
    async def recv():
        await asyncio.Event().wait()

    socket.recv = recv
    with pytest.raises(RuntimeError, match="storage failed"):
        await asyncio.wait_for(stream._receive(socket, writer), 1)


@pytest.mark.asyncio
async def test_queue_overflow_invalidates_freshness_and_reports_recovery(monkeypatch, caplog):
    from websockets.asyncio import client

    from strategy.orb import store

    monkeypatch.setattr(store, "read_runtime_session", lambda day: {"plans": {"AAPL": {}}})
    monkeypatch.setattr(stream, "STREAM_QUEUE_SIZE", 1)
    frames = iter(
        [
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "bars": ["AAPL"], "updatedBars": ["AAPL"]}],
            [{"T": "b", "S": "AAPL"}, {"T": "b", "S": "AAPL"}],
        ]
    )

    class Socket:
        async def send(self, value):
            pass

        async def recv(self):
            return json.dumps(next(frames))

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    async def writer(queue, stop, feed):
        await stop.wait()

    async def sleep(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(client, "connect", lambda *a, **kw: Connection())
    monkeypatch.setattr(stream, "_write_stream", writer)
    monkeypatch.setattr(stream.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await stream._run("test-key", "test-secret")
    assert stream.STREAM_STATUS["error"] == "ALPACA_STREAM_QUEUE_FULL"
    assert not stream._connected and not stream._latest and not stream._completed
    assert "queue overflow" in caplog.text
    assert "test-key" not in caplog.text and "test-secret" not in caplog.text


@pytest.mark.asyncio
async def test_bulk_history_matches_individual_checks_and_uses_one_read(monkeypatch):
    from market_data.bar_store import save_bars
    from strategy.orb import retest_data

    plan, bars, now = scenario()
    other = plan.model_copy(update={"symbol": "MSFT"})
    monkeypatch.setattr(retest_data, "_coverage", {})
    save_bars(plan.source, plan.symbol, bars)
    save_bars(other.source, other.symbol, [bars[-1].model_copy(update={"symbol": other.symbol})])
    queries = []
    engine = get_sync_engine()

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "market_bars" in statement:
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = await retest_data.history_complete_many([plan, other], now=now)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result == {plan.symbol: True, other.symbol: False}
    assert len(queries) == 1
    assert result == {p.symbol: retest_data.history_complete(p, now=now) for p in [plan, other]}
