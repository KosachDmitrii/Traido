import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from strategy.orb import retest_data
from tests.unit.test_orb_retest import scenario


@pytest.fixture(autouse=True)
def clear_cache():
    from strategy.orb import runtime

    retest_data._cache.clear()
    retest_data._failures.clear()
    retest_data._cursor = 0
    runtime._pending_completed_bars.clear()
    runtime._last_ready_check = 0
    yield
    retest_data._cache.clear()
    retest_data._failures.clear()
    runtime._pending_completed_bars.clear()


@pytest.mark.asyncio
async def test_concurrent_observers_join_the_same_pass(monkeypatch):
    """The scanner must receive real counts while the watch loop is observing."""
    from strategy.orb import runtime

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def one_pass(context=None):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"wait_for_entry": 7}

    monkeypatch.setattr(runtime, "_observe_once", one_pass)
    runtime._observation_task = None
    first = asyncio.create_task(runtime.observe(SimpleNamespace(name="watch")))
    await started.wait()
    second = asyncio.create_task(runtime.observe(SimpleNamespace(name="scanner")))
    await asyncio.sleep(0)
    release.set()

    assert await first == {"wait_for_entry": 7}
    assert await second == {"wait_for_entry": 7}
    assert calls == 1
    assert runtime._observation_task is None


@pytest.mark.asyncio
async def test_completed_non_breakout_bar_updates_phase_without_full_pass(monkeypatch):
    from datetime import UTC, datetime
    from decimal import Decimal

    from core.enums import Timeframe
    from core.schemas import Bar
    from strategy.orb import runtime
    from strategy.orb.store import create_session, list_decisions, read_session

    plan, _, _ = scenario()
    bar = Bar(
        symbol=plan.symbol,
        timeframe=Timeframe.M5,
        ts=plan.range_end,
        open=plan.range_high - Decimal("0.10"),
        high=plan.range_high + Decimal("0.10"),
        low=plan.range_high - Decimal("0.20"),
        close=plan.range_high,
        volume=Decimal(100000),
        source="alpaca",
    )
    instant = bar.ts + timedelta(minutes=5, seconds=2)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz or UTC)

    monkeypatch.setattr(runtime, "datetime", Clock)
    create_session(
        plan.session,
        {
            "session": plan.session,
            "plans": {plan.symbol: plan.model_dump(mode="json")},
            "states": {plan.symbol: {"state": "WAIT", "reasons": ["ORB_RETEST_WAIT_BREAKOUT"]}},
        },
    )
    runtime.notify_completed_bar(bar)
    assert await runtime.observe_priority(SimpleNamespace()) == {"wait_for_entry": 1}
    state = read_session(plan.session)["states"][plan.symbol]
    assert state["reasons"] == ["ORB_RETEST_WAIT_BREAKOUT"]
    assert state["last_bar"]["close"] == str(plan.range_high)
    assert state["processing_lag_seconds"] == 2
    history = list_decisions(plan.session, plan.symbol)
    assert len(history) == 1
    assert history[0]["payload"]["last_bar"]["ts"] == bar.ts.isoformat()


def test_persisted_orb_session_restores_real_agent_statuses(monkeypatch):
    from core.activity import AgentActivityBoard
    from strategy.orb import runtime

    board = AgentActivityBoard()
    monkeypatch.setattr(runtime, "BOARD", board)
    runtime._restore_session_board(
        {
            "counts": {"eligible": 123},
            "plans": {"AAPL": {}, "MSFT": {}},
        }
    )

    agents = {item["id"]: item for item in board.snapshot()["agents"]}
    assert agents["universe"]["status"] == "done"
    assert agents["universe"]["updated_at"] is not None
    assert agents["structure"]["detail"] == "Opening ranges ready · 2 selected"
    assert agents["risk_plan"]["detail"] == "ORB geometry stored · 2 plans"
    assert agents["context"]["detail"] == "Waiting for a confirmed ORB entry"


@pytest.mark.asyncio
async def test_batch_reuses_completed_bars_but_approval_reads_fresh():
    plan, rows, now = scenario()
    feed = SimpleNamespace(
        get_bars_batch=AsyncMock(return_value={plan.symbol: rows}),
        get_bars=AsyncMock(
            side_effect=lambda s, t, start, end: [b for b in rows if start <= b.ts <= end]
        ),
    )
    await retest_data.prime_bars(feed, [plan], now=now)
    assert (
        await retest_data.read_bars(feed, plan, now=now + timedelta(seconds=50), cached=True)
        == rows
    )
    feed.get_bars.assert_not_awaited()
    await retest_data.prime_bars(feed, [plan], now=now + timedelta(seconds=50))
    feed.get_bars_batch.assert_awaited_once()
    await retest_data.read_bars(feed, plan, now=now, cached=False)
    feed.get_bars.assert_awaited_once()
    await retest_data.prime_bars(feed, [plan], now=now + timedelta(minutes=5))
    assert feed.get_bars_batch.await_count == 2


@pytest.mark.asyncio
async def test_missing_batch_member_is_not_fabricated_and_retried():
    plan, rows, now = scenario()
    feed = SimpleNamespace(get_bars_batch=AsyncMock(return_value={}), get_bars=AsyncMock())
    await retest_data.prime_bars(feed, [plan], now=now)
    with pytest.raises(ValueError, match="ORB_RETEST_HISTORY_GAP"):
        await retest_data.read_bars(feed, plan, now=now, cached=True)
    feed.get_bars_batch.return_value = {plan.symbol: rows}
    await retest_data.prime_bars(feed, [plan], now=now + timedelta(seconds=6))
    assert feed.get_bars_batch.await_count == 2


@pytest.mark.asyncio
async def test_provider_outage_does_not_fan_out_to_every_symbol():
    plan, _, now = scenario()
    error = httpx.HTTPStatusError(
        "upstream unavailable",
        request=httpx.Request("GET", "https://example.test"),
        response=httpx.Response(504),
    )
    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=error))
    for _ in range(3):
        await retest_data.prime_bars(feed, [plan], now=now)
        with pytest.raises(httpx.HTTPStatusError):
            await retest_data.read_bars(feed, plan, now=now, cached=True)
    feed.get_bars_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_observation_blocks_outage_clears_prices_and_resumes(monkeypatch):
    from datetime import UTC, datetime

    from core.activity import BOARD
    from database.models.desk import OrderIntentRow
    from database.session import session_factory
    from strategy.orb import runtime
    from strategy.orb.store import create_session, read_session

    plan, rows, now = scenario()

    class Clock(datetime):
        current: datetime

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz or UTC)

    Clock.current = now
    monkeypatch.setattr(runtime, "datetime", Clock)
    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=RuntimeError("offline")))
    broker = SimpleNamespace(place_order=AsyncMock())
    ctx = SimpleNamespace(market_data=feed, broker=broker)
    create_session(
        plan.session,
        {
            "session": plan.session,
            "feed": "sip",
            "plans": {plan.symbol: plan.model_dump(mode="json")},
            "states": {
                plan.symbol: {
                    "state": "WAIT",
                    "ask": "100",
                    "bid": "99",
                    "quote_at": now.isoformat(),
                }
            },
        },
    )

    async def evaluate_read(symbol, ctx):
        await retest_data.read_bars(ctx.market_data, plan, now=Clock.current, cached=True)
        return SimpleNamespace(status="wait_for_entry")

    evaluate = AsyncMock(side_effect=evaluate_read)
    monkeypatch.setattr(runtime, "evaluate_symbol", evaluate)
    before_events = len(BOARD.events)
    assert await runtime.observe(ctx) == {"data_blocked": 1}
    blocked_events = [
        event
        for event in BOARD.events[before_events:]
        if event.symbol == plan.symbol and event.message.startswith("ORB observation unavailable:")
    ]
    assert [event.message for event in blocked_events] == [
        "ORB observation unavailable: ORB_SERVICE_UNAVAILABLE"
    ]
    state = read_session(plan.session)["states"][plan.symbol]
    assert state["state"] == "DATA_BLOCKED"
    assert state["ask"] is None and state["bid"] is None and state["quote_at"] is None
    Clock.current = now + timedelta(seconds=5)
    assert await runtime.observe(ctx) == {"data_blocked": 1}
    assert not [
        event
        for event in BOARD.events[before_events + len(blocked_events) :]
        if event.symbol == plan.symbol and event.message.startswith("ORB observation unavailable:")
    ]
    feed.get_bars_batch.assert_awaited_once()
    assert evaluate.await_count == 2
    broker.place_order.assert_not_awaited()
    with session_factory()() as db:
        assert db.query(OrderIntentRow).count() == 0
    feed.get_bars_batch.side_effect = None
    feed.get_bars_batch.return_value = {plan.symbol: rows}
    Clock.current = now + timedelta(seconds=31)
    monkeypatch.setattr(retest_data, "_failures", {})
    assert await runtime.observe(ctx) == {"wait_for_entry": 1}
    assert evaluate.await_count == 3
