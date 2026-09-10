"""P1-9: one broker per cycle, not one per symbol.

`run_symbol_pipeline` built a broker and a market-data port for every symbol,
and neither factory caches. Against Alpaca that is sixty sets of HTTP clients
and sixty reads of the same account inside a few seconds. Against IBKR — a
stateful TWS socket carrying a client id — it is not a cost but a refusal, which
is why this blocked Paper certification rather than merely slowing things down.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.schemas import PortfolioSnapshot
from trading import scan_context as ctx_mod


def _snapshot(equity: Decimal = Decimal(100_000)) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=equity,
        cash=equity,
        buying_power=equity,
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
    )


class _CountingBroker:
    def __init__(self) -> None:
        self.portfolio_reads = 0
        self.closed = 0

    async def get_portfolio(self) -> PortfolioSnapshot:
        self.portfolio_reads += 1
        return _snapshot()

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch):
    built = SimpleNamespace(brokers=0, feeds=0, last=None)

    def _broker(_settings):
        built.brokers += 1
        built.last = _CountingBroker()
        return built.last

    def _feed(_settings):
        built.feeds += 1
        return object()

    monkeypatch.setattr(ctx_mod, "create_broker", _broker)
    monkeypatch.setattr(ctx_mod, "create_market_data_port", _feed)
    monkeypatch.setattr(ctx_mod, "is_kill_switch_on", lambda: False)
    return built


async def test_a_cycle_over_many_symbols_builds_one_broker(counted) -> None:
    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        for _ in range(60):
            await cycle.portfolio()

    assert counted.brokers == 1, f"one connection per cycle, built {counted.brokers}"
    assert counted.feeds == 1


async def test_the_account_is_read_once_and_shared(counted) -> None:
    """Sixty reads across a cycle are sixty different portfolios.

    The first symbol's risk verdict would be judged against a different account
    state than the last, and the position count each was checked against could
    differ by a fill that landed mid-cycle — while the whole point of ranking
    them afterwards is that they are comparable.
    """
    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        for _ in range(10):
            await cycle.portfolio()

        assert cycle.broker.portfolio_reads == 1


async def test_a_refresh_re_reads_deliberately(counted) -> None:
    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        await cycle.portfolio()
        await cycle.portfolio(refresh=True)

        assert cycle.broker.portfolio_reads == 2


async def test_the_kill_switch_is_re_read_every_time(
    counted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halt pressed mid-cycle must stop the symbols still to be scanned.

    Everything else about the account is deliberately frozen for the cycle; this
    one thing cannot be, or a kill switch would take effect a cycle late.
    """
    halted = False
    monkeypatch.setattr(ctx_mod, "is_kill_switch_on", lambda: halted)

    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        assert (await cycle.portfolio()).kill_switch is False
        halted = True
        assert (await cycle.portfolio()).kill_switch is True


async def test_the_connection_is_closed_when_the_cycle_ends(counted) -> None:
    """An IBKR socket left open per cycle exhausts the client-id space."""
    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        broker = cycle.broker

    assert broker.closed == 1


async def test_a_failing_cycle_still_closes_the_connection(counted) -> None:
    broker = None
    with pytest.raises(RuntimeError):
        async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
            broker = cycle.broker
            raise RuntimeError("one bad symbol")

    assert broker is not None and broker.closed == 1


async def test_a_broker_that_cannot_close_does_not_fail_the_cycle(
    counted, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _bad_close() -> None:
        raise RuntimeError("socket already gone")

    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        monkeypatch.setattr(cycle.broker, "aclose", _bad_close)


async def test_an_adapter_without_a_close_method_is_fine(
    counted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpaca is stateless HTTP and has nothing to close."""

    class _Stateless:
        async def get_portfolio(self) -> PortfolioSnapshot:
            return _snapshot()

    monkeypatch.setattr(ctx_mod, "create_broker", lambda _s: _Stateless())

    async with ctx_mod.open_scan_context(SimpleNamespace()) as cycle:
        await cycle.portfolio()


def test_the_scanner_opens_exactly_one_context_per_cycle(monkeypatch):
    from agents.scanner import cycle as scan_cycle
    from agents.scanner.cycle import run_cycle
    from strategy.orb import runtime
    from tests.scanner_fakes import fake_scan_context, scanner_settings, universe_service_for

    contexts = []
    seen = []

    def opened(settings=None, **kwargs):
        ctx = fake_scan_context(settings)
        contexts.append(ctx)
        return ctx

    async def discover(ctx, universe):
        seen.append(ctx)
        return {"status": "ready", "plans": {}, "counts": {}}

    async def observe(context=None):
        seen.append(context)
        return {}

    monkeypatch.setattr(scan_cycle, "open_scan_context", opened)
    monkeypatch.setattr(runtime, "discover", discover)
    monkeypatch.setattr(runtime, "observe", observe)
    asyncio.run(
        run_cycle(settings=scanner_settings(), universe_service=universe_service_for(["AAA"]))
    )
    assert len(contexts) == 1
    assert seen == [contexts[0], contexts[0]]
