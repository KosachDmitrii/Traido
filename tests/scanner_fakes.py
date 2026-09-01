"""Deterministic vendors for driving a whole scan cycle in a test.

The scanner's properties that matter most at a thousand names — that ranking
does not depend on completion order, that the funnel balances, that only
finalists reach expensive analysis — cannot be checked against a live provider,
because a live provider's timing is the very thing being ruled out.

So: a universe generated from a seed, a market-data feed that answers from that
seed, and an optional shuffle of completion order that changes *when* answers
arrive without changing *what* they say. A test that passes under every shuffle
has shown the result is a function of the data and nothing else.

CI never touches the network as a result, which is also the point of Phase 19's
benchmark numbers being reproducible.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.concurrency import DEFAULT_BUDGETS, AIBudget, ConcurrencyManager, ResourceBudget
from core.config import Settings
from core.enums import Timeframe
from core.schemas import Bar, PortfolioSnapshot, Snapshot
from trading.scan_context import ScanContext
from universe.models import AssetClass, Instrument, UniverseTier
from universe.provider import StaticUniverseProvider
from universe.service import UniverseService


def flat_portfolio() -> PortfolioSnapshot:
    """An account holding nothing, with every field the model requires.

    Spelled out rather than relying on defaults: `PortfolioSnapshot` has none,
    deliberately, so that a partially-built account cannot reach the risk engine
    looking complete.
    """
    return PortfolioSnapshot(
        equity=Decimal(100000),
        cash=Decimal(100000),
        buying_power=Decimal(100000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
    )


def make_symbol(index: int) -> str:
    """A unique, plain, uppercase ticker for index `n`.

    Plain and alphabetic because Stage 0 rejects anything else on identity — a
    generator that emitted `SYM0001` would produce a universe that is 100 %
    structurally ineligible and a test that proves nothing.
    """
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    n = index
    for _ in range(4):
        out.append(letters[n % 26])
        n //= 26
    return "".join(reversed(out))


class FakeUniverseProvider:
    """`count` instruments, all structurally eligible unless asked otherwise."""

    def __init__(
        self,
        count: int,
        *,
        name: str = "fake",
        otc_every: int = 0,
        inactive_every: int = 0,
    ) -> None:
        self._name = name
        self.calls = 0
        now = datetime.now(UTC)
        self._instruments: list[Instrument] = []
        for i in range(count):
            self._instruments.append(
                Instrument(
                    symbol=make_symbol(i),
                    asset_class=AssetClass.ETF if i % 50 == 0 else AssetClass.STOCK,
                    exchange="NASDAQ",
                    currency="USD",
                    active=not (inactive_every and i % inactive_every == 0),
                    tradable=True,
                    otc=bool(otc_every and i % otc_every == 0),
                    provider=name,
                    as_of=now,
                )
            )

    @property
    def name(self) -> str:
        return self._name

    async def get_universe(self, *, tier: UniverseTier = UniverseTier.CORE) -> list[Instrument]:
        self.calls += 1
        return list(self._instruments)


class FakeMarketData:
    """Snapshots and daily bars derived from the symbol itself.

    Deterministic by construction: the same symbol always produces the same
    series, on any machine, in any order. `strongest` is served a materially
    better trend so a test can assert it wins from any position in the universe.
    """

    source = "fake"

    def __init__(
        self,
        *,
        strongest: str | None = None,
        illiquid: set[str] | None = None,
        missing: set[str] | None = None,
        shuffle_seed: int | None = None,
        now: datetime | None = None,
    ) -> None:
        self.strongest = (strongest or "").upper()
        self.illiquid = {s.upper() for s in (illiquid or set())}
        self.missing = {s.upper() for s in (missing or set())}
        self._rng = random.Random(shuffle_seed) if shuffle_seed is not None else None
        self._now = now or datetime.now(UTC)
        self.snapshot_requests = 0
        self.snapshot_symbols = 0
        self.bar_requests = 0
        self.bar_symbols = 0
        self.per_symbol_bar_calls: list[str] = []

    async def _maybe_delay(self) -> None:
        """Vary completion order without varying the answers.

        This is the mechanism behind the random-order regression: if any result
        depended on which worker finished first, a shuffled run would rank
        differently from an unshuffled one.
        """
        if self._rng is None:
            return
        await asyncio.sleep(self._rng.random() / 1000.0)

    def _price(self, symbol: str) -> float:
        return 20.0 + (sum(ord(c) for c in symbol) % 400)

    async def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        await self._maybe_delay()
        self.snapshot_requests += 1
        self.snapshot_symbols += len(symbols)
        out: dict[str, Snapshot] = {}
        for raw in symbols:
            symbol = raw.upper()
            if symbol in self.missing:
                continue
            price = self._price(symbol)
            if symbol in self.illiquid:
                volume = 5_000.0
            elif symbol == self.strongest:
                # The strongest name is liquid as well as trending. Stage 1 cuts
                # by traded value before Stage 2 ever sees a trend, so a
                # "strongest candidate" that is thin would be rejected on
                # liquidity and the regression would be testing the prefilter
                # rather than the ranking.
                volume = 20_000_000.0
            else:
                volume = 3_000_000.0
            out[symbol] = Snapshot(
                symbol=symbol,
                price=Decimal(str(round(price, 2))),
                bid=Decimal(str(round(price * 0.9995, 2))),
                ask=Decimal(str(round(price * 1.0005, 2))),
                day_volume=Decimal(str(volume)),
                day_high=Decimal(str(round(price * 1.01, 2))),
                day_low=Decimal(str(round(price * 0.99, 2))),
                prev_close=Decimal(str(round(price * 0.995, 2))),
                trade_ts=self._now,
                quote_ts=self._now,
                source=self.source,
            )
        return out

    def _series(self, symbol: str, bars: int = 120) -> list[Bar]:
        base = self._price(symbol)
        # A gentle drift for everyone, a much stronger one for the chosen name,
        # so the winner is a fact about the data rather than about the ordering.
        drift = 0.004 if symbol == self.strongest else 0.0002
        wobble = (sum(ord(c) for c in symbol) % 7) / 10000.0
        out: list[Bar] = []
        for i in range(bars):
            close = base * (1.0 + drift * i + wobble * (i % 5))
            ts = self._now - timedelta(days=bars - i)
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=Timeframe.D1,
                    ts=ts,
                    open=Decimal(str(round(close * 0.995, 4))),
                    high=Decimal(str(round(close * 1.01, 4))),
                    low=Decimal(str(round(close * 0.99, 4))),
                    close=Decimal(str(round(close, 4))),
                    volume=Decimal(2500000),
                    source=self.source,
                )
            )
        return out

    async def get_daily_bars_batch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[Bar]]:
        await self._maybe_delay()
        self.bar_requests += 1
        self.bar_symbols += len(symbols)
        return {
            s.upper(): self._series(s.upper()) for s in symbols if s.upper() not in self.missing
        }

    async def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        # Recorded so a test can assert that the *expensive* per-symbol path is
        # reached only by finalists.
        self.per_symbol_bar_calls.append(symbol.upper())
        await self._maybe_delay()
        return self._series(symbol.upper())

    async def get_last_price(self, symbol: str) -> float:
        return self._price(symbol.upper())


class FakeBroker:
    """An account that answers, and counts how often it was asked."""

    def __init__(self) -> None:
        self.portfolio_calls = 0

    async def get_portfolio(self) -> PortfolioSnapshot:
        self.portfolio_calls += 1
        return flat_portfolio()

    async def list_positions(self) -> list:
        return []

    async def aclose(self) -> None:
        return None


def static_universe(symbols: Sequence[str]) -> StaticUniverseProvider:
    """A provider over exactly these tickers, all structurally eligible."""
    now = datetime.now(UTC)
    return StaticUniverseProvider(
        [
            Instrument(
                symbol=s.upper(),
                asset_class=AssetClass.STOCK,
                exchange="NASDAQ",
                currency="USD",
                provider="test",
                as_of=now,
            )
            for s in symbols
        ],
        name="test",
    )


def scanner_settings(**overrides: object) -> Settings:
    """Real `Settings`, so a test cannot pass under a shape production rejects.

    Stage limits are opened up by default because a unit test's universe is
    three names and the production caps would silently do the filtering the test
    is trying to observe.
    """
    base: dict[str, object] = {
        "TRAIDO_UNIVERSE_MODE": "CORE",
        "TRAIDO_UNIVERSE_MAX_SIZE": 0,
        "TRAIDO_MARKET_PREFILTER_LIMIT": 0,
        "TRAIDO_QUANT_TOP_K": 100,
        "TRAIDO_DEEP_ANALYSIS_TOP_K": 100,
        "TRAIDO_MAX_LLM_CANDIDATES": 100,
        "TRAIDO_SCANNER_CONCURRENCY": 4,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def unpaced_budgets(workers: int = 4) -> dict[str, ResourceBudget]:
    """Production concurrency limits with the rate limiters switched off.

    Pacing is real and worth having in production — it is what stops a burst of
    finalists from tripping a vendor's per-second quota. In a test it is only
    wall-clock: twenty finalists at two per second is ten seconds of sleeping
    per cycle, and the suite runs the cycle dozens of times.

    Concurrency itself is left in place, because that is a semantic the tests
    genuinely check. The one test that asserts a peak in-flight count builds its
    own manager from the real budgets.
    """
    return {
        name: replace(budget, rate_per_sec=None, max_concurrency=workers)
        for name, budget in DEFAULT_BUDGETS.items()
    }


def fake_scan_context(
    settings: Settings | None = None,
    *,
    market_data: FakeMarketData | None = None,
    broker: FakeBroker | None = None,
) -> ScanContext:
    """A cycle context wired to deterministic vendors."""
    resolved = settings or scanner_settings()
    return ScanContext(
        settings=resolved,
        broker=broker or FakeBroker(),  # type: ignore[arg-type]
        market_data=market_data or FakeMarketData(),  # type: ignore[arg-type]
        concurrency=ConcurrencyManager(unpaced_budgets(resolved.scanner_concurrency)),
        ai_budget=AIBudget(
            max_candidates=resolved.max_llm_candidates,
            max_calls=resolved.max_llm_calls_per_scan,
        ),
    )


def universe_service_for(symbols: Sequence[str]) -> UniverseService:
    return UniverseService(static_universe(symbols), curated_provider=static_universe(symbols))
