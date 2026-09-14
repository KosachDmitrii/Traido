import asyncio
import json
import runpy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from websockets.asyncio import client

from core.clock import ET
from market_data import alpaca_stream
from market_data.bar_store import load_bars
from strategy.orb import retest_data, runtime
from strategy.orb.store import create_session
from tests.unit.test_durable_market_bars import minute
from tests.unit.test_orb_retest import scenario


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(retest_data, "_cache", {})
    monkeypatch.setattr(retest_data, "_failures", {})
    monkeypatch.setattr(alpaca_stream, "_latest", {})
    monkeypatch.setattr(alpaca_stream, "_completed", {})
    monkeypatch.setattr(alpaca_stream, "_connected", False)
    monkeypatch.setattr(alpaca_stream, "_connected_at", None)
    monkeypatch.setattr(alpaca_stream, "_symbol_limit", None)
    monkeypatch.setattr(alpaca_stream, "_feed", "sip")


def test_reconnected_stream_cannot_reuse_old_completion(monkeypatch):
    p, _, now = scenario()
    monkeypatch.setattr(alpaca_stream, "_connected", True)
    alpaca_stream._latest[p.symbol] = now
    assert not alpaca_stream.current(p.symbol, "alpaca:sip", now)
    alpaca_stream._completed[p.symbol] = now - timedelta(minutes=10)
    assert not alpaca_stream.current(p.symbol, "alpaca:sip", now)
    alpaca_stream._completed[p.symbol] = now - timedelta(minutes=5)
    assert alpaca_stream.current(p.symbol, "alpaca:sip", now)
    assert not alpaca_stream.current(p.symbol, "alpaca:other", now)


def test_current_accepts_only_the_connected_feed(monkeypatch):
    p, _, now = scenario()
    monkeypatch.setattr(alpaca_stream, "_feed", "sip")
    monkeypatch.setattr(alpaca_stream, "_connected", True)
    alpaca_stream._latest[p.symbol] = now
    alpaca_stream._completed[p.symbol] = now - timedelta(minutes=5)
    assert alpaca_stream.current(p.symbol, "alpaca:sip", now)
    assert not alpaca_stream.current(p.symbol, "alpaca:other", now)


def test_market_bar_migration_upgrade_and_downgrade():
    engine = create_engine("sqlite://")
    migration = runpy.run_path("alembic/versions/0019_market_bars.py")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration["upgrade"]()
        assert "market_bars" in inspect(conn).get_table_names()
        migration["downgrade"]()
        assert "market_bars" not in inspect(conn).get_table_names()


def test_orb_decision_event_migration_upgrade_and_downgrade():
    engine = create_engine("sqlite://")
    migration = runpy.run_path("alembic/versions/0020_orb_decision_events.py")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration["upgrade"]()
        assert "orb_decision_events" in inspect(conn).get_table_names()
        migration["downgrade"]()
        assert "orb_decision_events" not in inspect(conn).get_table_names()


@pytest.mark.asyncio
async def test_stream_handshake_subscription_and_shutdown(monkeypatch):
    now = datetime.now(UTC)
    start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) - timedelta(
        minutes=10
    )
    symbols = ["AAPL", "MSFT"]
    create_session(str(now.astimezone(ET).date()), {"plans": {s: {} for s in symbols}})
    frames = iter(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "bars": symbols, "updatedBars": symbols}],
            [minute(i, start) for i in range(5)],
        ]
    )
    sent = []

    class Socket:
        async def send(self, value):
            sent.append(json.loads(value))

        async def recv(self):
            try:
                return json.dumps(next(frames))
            except StopIteration:
                raise asyncio.CancelledError from None

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    calls = []

    def connect(url, **kwargs):
        calls.append(url)
        return Connection()

    monkeypatch.setattr(client, "connect", connect)
    with pytest.raises(asyncio.CancelledError):
        await alpaca_stream._run("test-key", "test-secret")
    assert calls == ["wss://stream.data.alpaca.markets/v2/sip"]
    assert sent[0]["action"] == "auth" and sent[1]["updatedBars"] == symbols
    assert sent[-1]["updatedBars"] == symbols
    assert len(load_bars("alpaca:sip", "AAPL", start, now)) == 1
    assert not alpaca_stream._connected and not alpaca_stream._latest
    assert not alpaca_stream._completed


@pytest.mark.asyncio
async def test_sip_stream_uses_sip_source_without_basic_symbol_limit(monkeypatch):
    now = datetime.now(UTC)
    start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) - timedelta(
        minutes=10
    )
    symbols = ["AAPL", "MSFT"]
    create_session(str(now.astimezone(ET).date()), {"plans": {s: {} for s in symbols}})
    frames = iter(
        [
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "bars": symbols, "updatedBars": symbols}],
            [minute(i, start) for i in range(5)],
        ]
    )

    class Socket:
        async def send(self, value):
            pass

        async def recv(self):
            try:
                return json.dumps(next(frames))
            except StopIteration:
                raise asyncio.CancelledError from None

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    calls = []

    def connect(url, **kwargs):
        calls.append(url)
        return Connection()

    monkeypatch.setattr(client, "connect", connect)
    with pytest.raises(asyncio.CancelledError):
        await alpaca_stream._run("test-key", "test-secret", "sip")
    assert calls == ["wss://stream.data.alpaca.markets/v2/sip"]
    assert len(load_bars("alpaca:sip", "AAPL", start, now)) == 1
    assert load_bars("alpaca:other", "AAPL", start, now) == []


@pytest.mark.asyncio
async def test_quote_display_failure_does_not_block_bar_evaluation(monkeypatch):
    p, rows, instant = scenario()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(runtime, "datetime", Clock)
    create_session(
        p.session, {"session": p.session, "plans": {p.symbol: p.model_dump(mode="json")}}
    )
    market_data = SimpleNamespace(
        get_bars_batch=AsyncMock(return_value={p.symbol: rows}),
        get_snapshots=AsyncMock(side_effect=TimeoutError()),
    )
    broker = SimpleNamespace(place_order=AsyncMock())
    ctx = SimpleNamespace(market_data=market_data, broker=broker)
    evaluate = AsyncMock(return_value=SimpleNamespace(status="wait_for_entry"))
    monkeypatch.setattr(runtime, "evaluate_symbol", evaluate)
    assert await runtime.observe(ctx) == {"wait_for_entry": 1}
    assert ctx.observation_snapshots == {}
    evaluate.assert_awaited_once()
    broker.place_order.assert_not_awaited()
