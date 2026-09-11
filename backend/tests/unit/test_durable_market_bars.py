from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.enums import Timeframe
from market_data import iex_stream
from market_data.bar_store import load_bars, save_bars
from strategy.orb import retest_data
from tests.unit.test_orb_retest import scenario


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(retest_data, "_cache", {})
    monkeypatch.setattr(retest_data, "_failures", {})
    monkeypatch.setattr(retest_data, "_cursor", 0)
    monkeypatch.setattr(retest_data, "_last_attempt", {})
    monkeypatch.setattr(retest_data, "_probe_after", {})
    monkeypatch.setattr(iex_stream, "_connected", False)
    monkeypatch.setattr(iex_stream, "_latest", {})
    monkeypatch.setattr(iex_stream, "_completed", {})
    monkeypatch.setattr(iex_stream, "_connected_at", None)
    monkeypatch.setattr(iex_stream, "_feed", "iex")


def test_upserts_corrections_and_separates_feed():
    p, rows, now = scenario()
    save_bars(p.source, p.symbol, rows)
    changed = rows[1].model_copy(update={"close": D("101.10")})
    save_bars(p.source, p.symbol, [changed])
    result = load_bars(p.source, p.symbol, p.range_end, now)
    assert len(result) == 3 and result[1].close == D("101.10")
    assert load_bars("alpaca:other", p.symbol, p.range_end, now) == []


@pytest.mark.asyncio
async def test_restart_dogloads_only_recent_overlap():
    p, rows, now = scenario()
    save_bars(p.source, p.symbol, rows)

    async def batch(symbols, start, end, timeframe):
        assert start == now - timedelta(minutes=10)
        return {p.symbol: [b for b in rows if start <= b.ts <= end]}

    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=batch))
    await retest_data.prime_bars(feed, [p], now=now)
    assert await retest_data.read_bars(feed, p, now=now, cached=True) == rows
    feed.get_bars_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_gap_recovery_never_refetches_prefix():
    p, rows, now = scenario()
    save_bars(p.source, p.symbol, [rows[0], rows[2]])

    async def batch(symbols, start, end, timeframe):
        assert start == rows[1].ts
        return {p.symbol: rows[1:]}

    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=batch))
    await retest_data.prime_bars(feed, [p], now=now)
    assert await retest_data.read_bars(feed, p, now=now, cached=True) == rows


@pytest.mark.asyncio
async def test_failed_group_does_not_discard_healthy_group():
    p, rows, now = scenario()
    plans = [p.model_copy(update={"symbol": f"S{i}"}) for i in range(10)]

    async def batch(symbols, start, end, timeframe):
        if "S0" in symbols:
            raise TimeoutError()
        return {s: [b.model_copy(update={"symbol": s}) for b in rows] for s in symbols}

    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=batch))
    await retest_data.prime_bars(feed, plans, now=now)
    with pytest.raises(TimeoutError):
        await retest_data.read_bars(feed, plans[0], now=now, cached=True)
    assert len(await retest_data.read_bars(feed, plans[-1], now=now, cached=True)) == 3
    assert feed.get_bars_batch.await_count == 2
    assert all(len(call.args[0]) <= 5 for call in feed.get_bars_batch.await_args_list)


@pytest.mark.asyncio
async def test_requests_bounded_and_failed_groups_shrink():
    p, _, now = scenario()
    plans = [p.model_copy(update={"symbol": f"S{i}"}) for i in range(54)]
    feed = SimpleNamespace(get_bars_batch=AsyncMock(side_effect=TimeoutError()))
    await retest_data.prime_bars(feed, plans, now=now + timedelta(hours=3))
    assert feed.get_bars_batch.await_count == 6
    for call in feed.get_bars_batch.await_args_list:
        assert call.args[2] - call.args[1] < timedelta(minutes=30)
    await retest_data.prime_bars(feed, plans[:5], now=now + timedelta(hours=3, seconds=31))
    assert all(len(call.args[0]) == 1 for call in feed.get_bars_batch.await_args_list[6:])


def minute(i, start, close=101):
    return {
        "T": "b",
        "S": "AAPL",
        "t": (start + timedelta(minutes=i)).isoformat(),
        "o": 100,
        "h": 102,
        "l": 99,
        "c": close,
        "v": 10,
    }


@pytest.mark.asyncio
async def test_old_missing_candle_does_not_consume_every_recovery_pass():
    p, _, now = scenario()
    now += timedelta(hours=3)
    feed = SimpleNamespace(get_bars_batch=AsyncMock(return_value={}))
    await retest_data.prime_bars(feed, [p], now=now)
    await retest_data.prime_bars(feed, [p], now=now + timedelta(seconds=31))
    feed.get_bars_batch.assert_awaited_once()
    with pytest.raises(ValueError, match="ORB_RETEST_HISTORY_GAP"):
        await retest_data.read_bars(feed, p, now=now, cached=True)


def test_stream_requires_all_minutes_and_applies_late_corrections():
    p, _, now = scenario()
    start = p.range_end
    for i in [0, 1, 3, 4]:
        assert iex_stream.ingest(minute(i, start), now=now)
    assert load_bars("alpaca:iex", p.symbol, start, now) == []
    assert iex_stream.ingest(minute(2, start), now=now)
    bars = load_bars("alpaca:iex", p.symbol, start, now)
    assert len(bars) == 1 and bars[0].volume == 50 and bars[0].timeframe == Timeframe.M5
    update = minute(4, start, 102)
    update["T"] = "u"
    iex_stream.ingest(update, now=now)
    assert load_bars("alpaca:iex", p.symbol, start, now)[0].close == 102


def test_stream_rejects_unclosed_or_invalid_source_minute():
    p, _, now = scenario()
    assert not iex_stream.ingest(minute(0, now), now=now)
    bad = minute(0, p.range_end)
    bad["h"] = 50
    assert not iex_stream.ingest(bad, now=now)
    assert load_bars("alpaca:iex", p.symbol, p.range_end, now) == []


@pytest.mark.asyncio
async def test_complete_live_stream_survives_rest_outage_but_stale_stream_cannot_authorize(
    monkeypatch,
):
    p, rows, now = scenario()
    p = p.model_copy(update={"source": "alpaca:iex"})
    save_bars(p.source, p.symbol, rows)
    monkeypatch.setattr(iex_stream, "_connected", True)
    iex_stream._latest[p.symbol] = now
    iex_stream._completed[p.symbol] = now - timedelta(minutes=5)
    feed = SimpleNamespace(get_bars=AsyncMock(side_effect=TimeoutError()))
    assert await retest_data.read_bars(feed, p, now=now, cached=False) == rows
    feed.get_bars.assert_not_awaited()
    iex_stream._latest[p.symbol] = now - timedelta(minutes=2)
    with pytest.raises(TimeoutError):
        await retest_data.read_bars(feed, p, now=now, cached=False)
    feed.get_bars.assert_awaited_once()
