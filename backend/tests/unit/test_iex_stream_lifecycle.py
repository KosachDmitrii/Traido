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
from market_data import iex_stream
from market_data.bar_store import load_bars
from strategy.orb import retest_data, runtime
from strategy.orb.store import create_session
from tests.unit.test_durable_market_bars import minute
from tests.unit.test_orb_retest import scenario


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(retest_data, "_cache", {})
    monkeypatch.setattr(retest_data, "_failures", {})
    monkeypatch.setattr(iex_stream, "_latest", {})
    monkeypatch.setattr(iex_stream, "_completed", {})
    monkeypatch.setattr(iex_stream, "_connected", False)
    monkeypatch.setattr(iex_stream, "_connected_at", None)
    monkeypatch.setattr(iex_stream, "_symbol_limit", None)


def test_reconnected_stream_cannot_reuse_old_completion(monkeypatch):
    p, _, now = scenario()
    monkeypatch.setattr(iex_stream, "_connected", True)
    iex_stream._latest[p.symbol] = now
    assert not iex_stream.current(p.symbol, "alpaca:iex", now)
    iex_stream._completed[p.symbol] = now - timedelta(minutes=10)
    assert not iex_stream.current(p.symbol, "alpaca:iex", now)
    iex_stream._completed[p.symbol] = now - timedelta(minutes=5)
    assert iex_stream.current(p.symbol, "alpaca:iex", now)
    assert not iex_stream.current(p.symbol, "alpaca:sip", now)


def test_market_bar_migration_upgrade_and_downgrade():
    engine = create_engine("sqlite://")
    migration = runpy.run_path("alembic/versions/0019_market_bars.py")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration["upgrade"]()
        assert "market_bars" in inspect(conn).get_table_names()
        migration["downgrade"]()
        assert "market_bars" not in inspect(conn).get_table_names()


@pytest.mark.asyncio
@pytest.mark.parametrize("limited", [False, True])
async def test_stream_handshake_subscription_and_shutdown(monkeypatch, limited):
    now = datetime.now(UTC)
    start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) - timedelta(
        minutes=10
    )
    symbols = ["AAPL"] + ([f"S{i}" for i in range(53)] if limited else [])
    selected = symbols[:30]
    create_session(str(now.astimezone(ET).date()), {"plans": {s: {} for s in symbols}})
    frames = iter(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            *([[{"T": "error", "code": 405}]] if limited else []),
            [{"T": "subscription", "bars": selected, "updatedBars": selected}],
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
        await iex_stream._run("test-key", "test-secret")
    assert calls == ["wss://stream.data.alpaca.markets/v2/iex"]
    assert sent[0]["action"] == "auth" and sent[1]["updatedBars"] == symbols
    assert sent[-1]["updatedBars"] == selected
    assert len(load_bars("alpaca:iex", "AAPL", start, now)) == 1
    assert not iex_stream._connected and not iex_stream._latest and not iex_stream._completed


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
