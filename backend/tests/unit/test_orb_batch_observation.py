from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from strategy.orb import retest_data
from tests.unit.test_orb_retest import scenario


@pytest.fixture(autouse=True)
def clear_cache():
    retest_data._cache.clear()
    retest_data._failures.clear()
    yield
    retest_data._cache.clear()
    retest_data._failures.clear()


@pytest.mark.asyncio
async def test_batch_reuses_completed_bars_but_approval_reads_fresh():
    plan, rows, now = scenario()
    feed = SimpleNamespace(
        get_bars_batch=AsyncMock(return_value={plan.symbol: rows}),
        get_bars=AsyncMock(return_value=rows),
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
    assert await retest_data.read_bars(feed, plan, now=now, cached=True) == []
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
        with pytest.raises(httpx.HTTPStatusError):
            await retest_data.prime_bars(feed, [plan], now=now)
    feed.get_bars_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_observation_blocks_outage_clears_prices_and_resumes(monkeypatch):
    from datetime import UTC, datetime

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
    monkeypatch.setattr(runtime, "_observation_error", None)
    monkeypatch.setattr(runtime, "_observation_retry_at", now - timedelta(seconds=1))
    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=RuntimeError("offline")))
    broker = SimpleNamespace(place_order=AsyncMock())
    ctx = SimpleNamespace(market_data=feed, broker=broker)
    create_session(
        plan.session,
        {
            "session": plan.session,
            "feed": "iex",
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
    evaluate = AsyncMock(return_value=SimpleNamespace(status="wait_for_entry"))
    monkeypatch.setattr(runtime, "evaluate_symbol", evaluate)
    assert await runtime.observe(ctx) == {"data_blocked": 1}
    state = read_session(plan.session)["states"][plan.symbol]
    assert state["state"] == "DATA_BLOCKED"
    assert state["ask"] is None and state["bid"] is None and state["quote_at"] is None
    Clock.current = now + timedelta(seconds=5)
    assert await runtime.observe(ctx) == {"data_blocked": 1}
    feed.get_bars_batch.assert_awaited_once()
    evaluate.assert_not_awaited()
    broker.place_order.assert_not_awaited()
    with session_factory()() as db:
        assert db.query(OrderIntentRow).count() == 0
    feed.get_bars_batch.side_effect = None
    feed.get_bars_batch.return_value = {plan.symbol: rows}
    Clock.current = now + timedelta(seconds=31)
    monkeypatch.setattr(retest_data, "_failures", {})
    assert await runtime.observe(ctx) == {"wait_for_entry": 1}
    evaluate.assert_awaited_once()
