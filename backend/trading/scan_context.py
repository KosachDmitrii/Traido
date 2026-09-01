"""The vendor connections one scan cycle shares.

`run_symbol_pipeline` called `create_broker` and `create_market_data_port` once
per symbol, and neither factory caches. A sixty-name cycle therefore built sixty
brokers and sixty market-data ports.

Against Alpaca that is wasteful — sixty sets of HTTP clients and sixty portfolio
reads of the same account, all inside a few seconds. Against IBKR it does not
work at all: the connection is a stateful TWS/Gateway socket with a client id,
and opening one per symbol either exhausts the client-id space or is refused
outright. This is the item that blocks Paper certification.

The context also fixes a subtler thing. Sixty portfolio reads across one cycle
are sixty *different* portfolios, so the risk verdict for the first symbol was
judged against a different account state than the last — and the position count
each was checked against could differ by a fill that landed mid-cycle. One read
per cycle makes the whole cycle's decisions comparable, which is what ranking
them against each other assumes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from broker.factory import create_broker
from core.concurrency import DEFAULT_BUDGETS, AIBudget, ConcurrencyManager, ResourceBudget
from core.config import Settings, get_settings
from core.enums import Timeframe
from core.ports import BatchMarketDataPort, BrokerPort, MarketDataPort, QuotePort
from core.schemas import Bar, PortfolioSnapshot, Snapshot
from market_data.factory import create_market_data_port
from risk.kill_switch import is_kill_switch_on

logger = logging.getLogger(__name__)


@dataclass
class ScanContext:
    """Vendors, account state and work budgets for the duration of one cycle.

    Carries an identity (`scan_id`) and its own clocks because a cycle is now a
    scheduled unit of work that can overrun, be measured and be reported on,
    rather than an anonymous pass through a list.
    """

    settings: Settings
    broker: BrokerPort
    market_data: MarketDataPort
    scan_id: UUID = field(default_factory=uuid4)
    scheduled_at: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    concurrency: ConcurrencyManager = field(default_factory=ConcurrencyManager)
    ai_budget: AIBudget = field(default_factory=AIBudget)
    _portfolio: PortfolioSnapshot | None = None
    _portfolio_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── Batched reads ───────────────────────────────────────────────────────

    async def snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        """Today's cheap picture for many symbols, batched if the feed can.

        Falls back to a bounded per-symbol loop when the provider has no batch
        capability. The fallback is correct and slow, which is the right way
        round: a provider without batching should make the desk take longer, not
        make it skip the filter and send everything to deep analysis.
        """
        wanted = [s.upper() for s in symbols if s]
        if not wanted:
            return {}
        feed = self.market_data
        if isinstance(feed, BatchMarketDataPort):
            return await self.concurrency.run("market_data", lambda: feed.get_snapshots(wanted))
        return await self._snapshots_one_by_one(wanted)

    async def _snapshots_one_by_one(self, symbols: list[str]) -> dict[str, Snapshot]:
        quoter = self.market_data if isinstance(self.market_data, QuotePort) else None
        if quoter is None:
            return {}

        async def _one(symbol: str) -> tuple[str, Snapshot | None]:
            quote = await quoter.get_quote(symbol)
            if quote is None:
                return symbol, None
            return symbol, Snapshot(
                symbol=symbol,
                price=(quote.bid + quote.ask) / 2,
                bid=quote.bid,
                ask=quote.ask,
                trade_ts=quote.ts,
                quote_ts=quote.ts,
                source=quote.source,
            )

        results = await self.concurrency.map("market_data", symbols, _one)
        out: dict[str, Snapshot] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            symbol, snap = result
            if snap is not None:
                out[symbol] = snap
        return out

    async def daily_bars(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[Bar]]:
        """Daily history for many symbols, batched if the feed can."""
        wanted = [s.upper() for s in symbols if s]
        if not wanted:
            return {}
        feed = self.market_data
        if isinstance(feed, BatchMarketDataPort):
            return await self.concurrency.run(
                "market_data",
                lambda: feed.get_daily_bars_batch(wanted, start, end),
            )

        async def _one(symbol: str) -> tuple[str, list[Bar]]:
            return symbol, await self.market_data.get_bars(symbol, Timeframe.D1, start, end)

        results = await self.concurrency.map("market_data", wanted, _one)
        out: dict[str, list[Bar]] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            symbol, bars = result
            out[symbol] = bars
        return out

    # ── Broker state, read once ─────────────────────────────────────────────

    async def portfolio(self, *, refresh: bool = False) -> PortfolioSnapshot:
        """The account as it stood when this cycle started.

        Read once and reused. The kill switch is re-read on every call, because
        it is the one piece of state that must take effect immediately: a halt
        pressed mid-cycle has to stop the symbols still to be scanned, not the
        next cycle.
        """
        # Single-flight, not merely cached. With Stage 3 running its finalists
        # concurrently, twenty coroutines reach the `is None` check before any
        # of them has finished reading, so a plain memo lets all twenty through
        # and the cycle makes twenty account requests — the exact behaviour this
        # context was built to remove, reintroduced by concurrency rather than
        # by a per-symbol factory.
        async with self._portfolio_lock:
            if self._portfolio is None or refresh:
                self._portfolio = await self.broker.get_portfolio()
            snapshot = self._portfolio
        return snapshot.model_copy(update={"kill_switch": is_kill_switch_on()})

    async def aclose(self) -> None:
        closer = getattr(self.broker, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:
            logger.warning("scan context: broker did not close cleanly", exc_info=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def open_scan_context(
    settings: Settings | None = None,
    *,
    scheduled_at: datetime | None = None,
) -> ScanContext:
    """Build the vendors and budgets for one cycle.

    One broker, one market-data port, one concurrency manager and one AI budget.
    The budgets are per cycle rather than per process so that a cycle's spend is
    its own and a semaphore cannot be held across cycles by a task that outlived
    the one that created it.
    """
    resolved = settings or get_settings()
    return ScanContext(
        settings=resolved,
        broker=create_broker(resolved),
        market_data=create_market_data_port(resolved),
        scheduled_at=scheduled_at,
        concurrency=ConcurrencyManager(
            budgets=_budgets_for(resolved),
        ),
        ai_budget=AIBudget(
            # `getattr` for the same reason as the concurrency knob below: these
            # are spend ceilings read from a settings object that tests and
            # benchmarks stub, and a missing one should take the documented
            # default rather than fail a cycle from three frames deep.
            max_candidates=int(getattr(resolved, "max_llm_candidates", 20)),
            max_calls=int(getattr(resolved, "max_llm_calls_per_scan", 60)),
        ),
    )


def _budgets_for(settings: Settings) -> dict[str, ResourceBudget]:
    """Per-resource budgets, with the operator-facing knob applied.

    `scanner_concurrency` moves news/market_data. It deliberately does not raise
    `deep` above one: each deep symbol paginates many hourly bar pages, and
    concurrent deep symbols are what turned a 200/min Alpaca quota into a 429
    storm. `broker` stays at one for the same reason as before — one socket.
    """
    budgets = {name: replace(budget) for name, budget in DEFAULT_BUDGETS.items()}
    # `getattr` because tests and benchmarks pass lightweight settings stubs, and
    # this is a throughput knob rather than a safety gate — a stub that omits it
    # should get the documented default, not an AttributeError one layer inside
    # a scan. Every gate that protects capital reads `settings` strictly.
    workers = max(1, int(getattr(settings, "scanner_concurrency", 2)))
    for resource in ("news", "market_data"):
        budgets[resource] = replace(budgets[resource], max_concurrency=workers)
    budgets["deep"] = replace(budgets["deep"], max_concurrency=1, rate_per_sec=1.0)
    return budgets
