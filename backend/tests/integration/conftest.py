"""The desk as it actually runs, with only the vendors replaced.

Why this layer exists at all is written down in
`docs/architecture/runtime-path-audit.md` §4.1: until now no test had ever
issued the two POSTs that move capital. Every gate was covered by a unit test
that built `ExecutionService` by hand, which proves the gate works when it is
called and says nothing about whether the route calls it. That gap is exactly
how the liquidity gate came to be written, tested, documented and never run.

So the rule here is narrow and worth stating: **replace only what is genuinely
external.** The FastAPI app, its routers, `api.deps`, `ExecutionService`, the
gates, the risk engine, the intent store, the ledger and the Alpaca adapter's
own normalization code are all the thing under test. What gets swapped is the
vendor's HTTP endpoint and the vendor's market data — nothing between the route
and the wire.

Broker mutations are counted at the HTTP boundary rather than by watching
`place_order`, because "zero broker mutations" is a claim about what left the
process, and only the transport can testify to that.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import broker.alpaca as alpaca_mod
from broker.alpaca import AlpacaPaperBroker
from core.enums import BrokerConnectionState, Timeframe, TradeAction, TradingMode
from core.ports import BrokerPort
from core.schemas import Bar, PortfolioSnapshot, Quote, TradeCandidate, TradeOpportunity
from risk.risk_engine import RiskEngine
from tests.contract.fakes import FakeAlpacaBackend
from tests.support import CLEARED_EARNINGS
from trading.reconcile_supervisor import RECONCILE

# ── The broker boundary ──────────────────────────────────────────────────────


@dataclass
class Mutation:
    """One thing that left the process heading for the broker."""

    kind: str
    """`place` or `cancel`."""
    symbol: str | None = None
    side: str | None = None
    order_type: str | None = None
    qty: str | None = None
    broker_order_id: str | None = None
    client_order_id: str | None = None


class RecordingBackend(FakeAlpacaBackend):
    """`FakeAlpacaBackend`, plus a tape of every mutating request.

    Subclassed rather than wrapped so the real `AlpacaPaperBroker` still builds
    the request, signs it and parses the reply. A test double placed above the
    adapter would not exercise any of that, and the adapter is production code.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mutations: list[Mutation] = []
        self.reject_order_types: set[str] = set()
        """Order types the venue refuses. `{"stop"}` makes a position naked."""
        self.open_orders_unreadable = False
        """The venue answers 500 to the open-order book — protection unverifiable."""
        self.drop_replies = 0
        """Accept the next N orders but lose the reply, as a timeout would."""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method

        if path == "/v2/orders" and method == "GET" and self.open_orders_unreadable:
            return httpx.Response(500, json={"message": "order book unavailable"})

        if path.startswith("/v2/orders/") and method == "DELETE":
            self.mutations.append(Mutation(kind="cancel", broker_order_id=path.rsplit("/", 1)[-1]))
            return super()._handle(request)

        if path != "/v2/orders" or method != "POST":
            return super()._handle(request)

        body = json.loads(request.content or b"{}")
        self.mutations.append(
            Mutation(
                kind="place",
                symbol=body.get("symbol"),
                side=body.get("side"),
                order_type=body.get("type"),
                qty=body.get("qty"),
                client_order_id=body.get("client_order_id"),
            )
        )

        if body.get("type") in self.reject_order_types:
            return httpx.Response(422, json={"message": "venue refuses this order type"})

        if self.drop_replies > 0:
            self.drop_replies -= 1
            # The venue accepts and records the order, then the answer is lost.
            # That asymmetry is the entire scenario: the desk knows only the
            # client id it chose, and has to find the order with it.
            super()._handle(request)
            raise httpx.ReadTimeout("reply lost after the venue accepted the order")

        return super()._handle(request)

    # ── Convenience views for assertions ─────────────────────────────────────

    @property
    def placed(self) -> list[Mutation]:
        return [m for m in self.mutations if m.kind == "place"]

    @property
    def canceled(self) -> list[Mutation]:
        return [m for m in self.mutations if m.kind == "cancel"]

    def placed_of_type(self, order_type: str) -> list[Mutation]:
        return [m for m in self.placed if m.order_type == order_type]


class DegradableBroker(AlpacaPaperBroker):
    """The real adapter with a settable link state.

    Alpaca is stateless REST and so reports READY by construction
    (`broker/interface.py:29`). The connectivity gate is real and sits on the
    entry path regardless, so proving it requires a broker that can say
    otherwise — this changes what the adapter reports, not how it behaves.
    """

    link_state: BrokerConnectionState = BrokerConnectionState.READY

    def connection_state(self) -> BrokerConnectionState:
        return self.link_state


# ── The market-data boundary ─────────────────────────────────────────────────


class ScriptedMarketData:
    """A symbol that clears the liquidity gate until a test says it should not.

    Quotes are stamped on the execution clock rather than the wall clock. The
    suite freezes "now" to a mid-session instant, so a wall-clock quote would
    be hours stale and every entry test would fail as `QUOTE_STALE` — correctly,
    and for a reason unrelated to what the test is asserting.
    """

    def __init__(self) -> None:
        self.price = 100.0
        self.volume = 5_000_000.0
        self.bars_available = True
        self.bar_count = 60
        self.quote_available = True
        self.quote_age_sec = 0.0
        self.spread_bps = 2.0
        self.custom_bid: float | None = None
        self.custom_ask: float | None = None
        self.raise_on_bars = False
        self.bar_age_days = 0.0
        """How far behind the clock the newest bar is. A feed that has stopped."""

    @staticmethod
    def _now() -> datetime:
        from trading import execution

        return execution._utcnow()

    async def get_quote(self, symbol: str) -> Quote | None:
        if not self.quote_available:
            return None
        if self.custom_bid is not None and self.custom_ask is not None:
            return Quote(
                symbol=symbol.upper(),
                bid=Decimal(str(self.custom_bid)),
                ask=Decimal(str(self.custom_ask)),
                ts=self._now() - timedelta(seconds=self.quote_age_sec),
                source="scripted",
            )
        mid = Decimal(str(self.price))
        half = mid * Decimal(str(self.spread_bps)) / Decimal(20_000)
        return Quote(
            symbol=symbol.upper(),
            bid=mid - half,
            ask=mid + half,
            ts=self._now() - timedelta(seconds=self.quote_age_sec),
            source="scripted",
        )

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        if self.raise_on_bars:
            raise RuntimeError("scripted market-data outage")
        if not self.bars_available:
            return []
        now = self._now()
        # D1 series can be aged for liquidity-gate tests. Exec timeframes (H1…)
        # must end at "now" so final admission is not confounded with ADV age.
        if timeframe == Timeframe.D1:
            stale = timedelta(days=self.bar_age_days)
            step = timedelta(days=1)
        else:
            stale = timedelta(0)
            step = timedelta(hours=1)
        bars: list[Bar] = []
        for i in range(self.bar_count):
            ts = now - stale - step * (self.bar_count - 1 - i)
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    ts=ts,
                    open=self.price,
                    high=self.price * 1.01,
                    low=self.price * 0.99,
                    close=self.price,
                    volume=self.volume,
                    source="scripted",
                )
            )
        return bars

    async def get_last_price(self, symbol: str) -> float:
        return self.price


# ── The desk under test ──────────────────────────────────────────────────────


@dataclass
class Desk:
    """Handle on a running desk: drive it over HTTP, read the broker's tape."""

    client: TestClient
    broker: DegradableBroker
    backend: RecordingBackend
    market: ScriptedMarketData
    seeded: list[TradeOpportunity] = field(default_factory=list)

    # ── Driving it ───────────────────────────────────────────────────────────

    def approve(self, opportunity_id: Any, *, request_id: Any | None = None) -> httpx.Response:
        from uuid import uuid4

        from trading.opportunities import OPPORTUNITIES

        opp = OPPORTUNITIES.get(opportunity_id)
        version = int(getattr(opp, "decision_version", 0) or 0) if opp else 0
        return self.client.post(
            f"/api/v1/opportunities/{opportunity_id}/decide",
            json={
                "decision": "approve",
                "request_id": str(request_id or uuid4()),
                "expected_decision_version": version,
            },
        )

    def sell(self, exit_id: Any) -> httpx.Response:
        return self.client.post(f"/api/v1/exits/{exit_id}/decide", json={"decision": "sell"})

    def run_in_app_loop(self, coro: Any) -> Any:
        """Drive one coroutine against the same wiring the app uses.

        For the background loop, which by definition has no HTTP request to ride
        on. A fresh event loop is safe here because the Alpaca adapter opens its
        HTTP client per call rather than holding one across the process.
        """
        import asyncio

        return asyncio.run(coro)

    def reconcile_now(self) -> None:
        """One pass through the supervisor, as the background loop makes it."""
        from api.deps import build_reconcile_pass
        from trading.reconcile_supervisor import RECONCILE

        self.run_in_app_loop(RECONCILE.run(build_reconcile_pass))

    def set_venue_holdings(self, symbol: str, qty: Decimal | float | None) -> None:
        """Change what the venue says it holds, as a fill or an assignment would.

        Clears the adapter's read cache as part of the change. That cache is
        module-global with a short TTL, so in reality a venue-side change is
        always separated from the next read by wall-clock time; a test that
        mutates the fake venue and reads back in the same millisecond would
        otherwise be asserting against the answer from before its own setup.
        """
        ticker = symbol.upper()
        if qty is None:
            self.backend.holdings.pop(ticker, None)
        elif ticker in self.backend.holdings:
            self.backend.holdings[ticker]["qty"] = float(qty)
        else:
            self.backend.holdings[ticker] = {
                "symbol": ticker,
                "qty": float(qty),
                "avg_entry_price": 100.0,
            }

    def age_reconciliation(self, seconds: float) -> None:
        """Pretend the last successful pass happened `seconds` ago.

        Reaches into the supervisor's monotonic stamp rather than sleeping,
        which is the only way to test a three-minute threshold in a test suite
        that has to finish in under a minute.
        """
        import time
        from dataclasses import replace

        from trading.reconcile_supervisor import RECONCILE

        status = RECONCILE.status
        assert status.last_success_mono is not None, "nothing to age: no pass has succeeded"
        RECONCILE._status = replace(
            status,
            last_success_mono=time.monotonic() - seconds,
            last_success_wall=time.time() - seconds,
        )

    def refresh_broker(self, *, fresh: bool = True) -> httpx.Response:
        return self.client.get(f"/api/v1/desk/broker?fresh={'true' if fresh else 'false'}")

    # ── Seeding ──────────────────────────────────────────────────────────────

    def offer(
        self,
        symbol: str = "AAPL",
        *,
        entry: float = 100.0,
        stop: float = 95.0,
        target: float = 115.0,
        confidence: float = 0.8,
    ) -> TradeOpportunity:
        """Put a real desk card in the real store, risk-decided by the real engine.

        Deliberately not a hand-built `TradeOpportunity`: the card a test
        approves should be the card the scanner would have published, so the
        risk snapshot on it is a genuine verdict against the fake broker's
        actual portfolio.
        """
        from tests.support import ensure_admission_ready
        from trading.opportunities import OPPORTUNITIES

        candidate = ensure_admission_ready(
            TradeCandidate(
                symbol=symbol.upper(),
                action=TradeAction.BUY,
                entry=Decimal(str(entry)),
                stop=Decimal(str(stop)),
                target=Decimal(str(target)),
                confidence=confidence,
                risk_reward=round((target - entry) / (entry - stop), 2),
                reasons=["integration harness"],
                strategy_version="integration@1",
                pipeline_run_id=uuid4(),
            )
        )
        # Read the account through the app's own route rather than awaiting the
        # broker here: the adapter's HTTP client belongs to the loop the test
        # client runs the app in, and reaching into it from a second loop is how
        # a harness starts testing itself instead of the desk.
        snap = self.client.get("/api/v1/portfolio")
        snap.raise_for_status()
        portfolio = PortfolioSnapshot.model_validate(snap.json())
        risk = RiskEngine().evaluate(candidate, portfolio, context=CLEARED_EARNINGS)
        opp = OPPORTUNITIES.create(candidate, risk, TradingMode.CONFIRMATION)
        self.seeded.append(opp)
        return opp

    def offer_exit(self, position_id: Any, symbol: str = "AAPL", *, current: float = 110.0) -> Any:
        """Put an exit card on the desk for a position the book already holds.

        The position agent builds these from live prices on a schedule, which a
        test cannot wait for. What matters downstream is the same either way:
        the card carries a position id, and `decide_exit` reads the position out
        of the ledger rather than trusting anything on the card.
        """
        from core.enums import UserDecision
        from core.schemas import ExitProposal
        from trading.exits import EXITS

        return EXITS.upsert(
            ExitProposal(
                position_id=position_id,
                symbol=symbol.upper(),
                entry=Decimal(100),
                current=Decimal(str(current)),
                pnl_pct=(current - 100.0),
                reasons=["integration harness"],
                recommendation=UserDecision.SELL,
                confidence=0.8,
            )
        )

    def open_position_id(self, symbol: str = "AAPL") -> Any:
        from trading.ledger import LEDGER

        row = LEDGER.find_open_by_symbol(symbol.upper())
        assert row is not None, f"no open position for {symbol}"
        return row.id

    # ── Breaking things at the venue, behind the book's back ─────────────────

    def strand_position(self) -> None:
        """Make the resting protective stop disappear, as a venue can.

        A stop is external state: it can be cancelled at the venue, dropped on
        an account change, or never have been native in the first place. The
        book still believes it is there, which is precisely the condition
        reconciliation exists to find.
        """
        for order in self.backend.orders.values():
            if order["side"] == "sell" and order["type"] == "stop":
                order["status"] = "canceled"

    def venue_holdings(self, symbol: str = "AAPL") -> float:
        held = self.backend.holdings.get(symbol.upper())
        return float(held["qty"]) if held else 0.0

    def plant_protective_sell(self, symbol: str, qty: Decimal, *, client_order_id: str) -> str:
        """Put a resting protective SELL at the venue that the book knows nothing about.

        Written straight into the fake venue rather than placed through the
        desk, because the desk is what is being tested: this is the state left
        behind by a duplicate pass, a partially-applied resize, or an order from
        a previous deployment, and none of those are reachable by asking the
        current code to misbehave.
        """
        order_id = str(uuid4())
        self.backend.orders[order_id] = {
            "id": order_id,
            "client_order_id": client_order_id,
            "symbol": symbol.upper(),
            "side": "sell",
            "type": "stop",
            "qty": str(qty),
            "status": "accepted",
            "limit_price": None,
            "stop_price": "90.0",
            "filled_qty": "0",
            "filled_avg_price": None,
        }
        return order_id

    def resting_protection(self, symbol: str = "AAPL") -> list[dict[str, Any]]:
        """Protective SELLs still live at the venue, whatever put them there."""
        return [
            o
            for o in self.backend.orders.values()
            if o["symbol"].upper() == symbol.upper()
            and o["side"] == "sell"
            and o["type"] == "stop"
            and o["status"] not in {"canceled", "filled", "expired", "rejected"}
        ]

    def assert_protection_never_exceeds_holdings(self, symbol: str = "AAPL") -> None:
        """The one invariant every protective failure mode violates.

        A duplicate stop, a stop left oversized after the venue shrank the
        position, and a stop orphaned above a position that was flattened are
        three different bugs with one observable consequence: more shares are
        promised to a resting SELL than the account holds. Whatever fires first
        sells shares that do not exist, which on a venue that permits it opens a
        short — and this desk's risk policy disables shorting.

        Asserted against the venue's own books rather than the tape, because the
        number of requests it took to get here is not the hazard.
        """
        held = Decimal(str(self.venue_holdings(symbol)))
        resting = self.resting_protection(symbol)
        promised = sum((Decimal(str(o["qty"])) for o in resting), Decimal(0))
        assert promised <= held, (
            f"{symbol}: venue holds {held} shares but resting protective stops "
            f"promise {promised} — "
            f"{[(o.get('client_order_id'), o['qty'], o['status']) for o in resting]}"
        )

    # ── Reading the tape ─────────────────────────────────────────────────────

    @property
    def mutations(self) -> list[Mutation]:
        return self.backend.mutations

    def assert_no_broker_mutations(self) -> None:
        assert self.backend.mutations == [], (
            f"the gate refused but something still reached the broker: {self.backend.mutations}"
        )


# ── Fixtures ─────────────────────────────────────────────────────────────────

_VENDOR_IMPORTERS = (
    "api.deps",
    "api.health",
    "api.routes.desk",
    "api.routes.evaluation",
    "api.routes.review",
    "api.routes.trading",
    "trading.pipeline",
)
"""Every module that binds a vendor factory by name. See the note in `desk`."""


def _substitute_vendors(
    monkeypatch: pytest.MonkeyPatch, *, broker: BrokerPort, market: ScriptedMarketData
) -> None:
    """Point every importer of a vendor factory at the fakes.

    Patching `broker.factory.create_broker` alone would miss all of them:
    each does `from broker.factory import create_broker`, so the name they
    call is their own.
    """
    import importlib

    for name in _VENDOR_IMPORTERS:
        module = importlib.import_module(name)
        if hasattr(module, "create_broker"):
            monkeypatch.setattr(module, "create_broker", lambda _s, _b=broker: _b)
        if hasattr(module, "create_market_data_port"):
            monkeypatch.setattr(module, "create_market_data_port", lambda _s, _m=market: _m)


@pytest.fixture(autouse=True)
def isolated_order_intents() -> Iterator[None]:
    """Override the unit suite's in-memory intent store.

    The root fixture swaps `INTENTS` for a memory store so one test's `UNKNOWN`
    cannot block the next. Here that would defeat the point: durability across a
    simulated restart is a property these tests exist to check, and a store that
    lives in a Python object cannot demonstrate it. Isolation comes from the
    per-test database below instead.
    """
    yield


@pytest.fixture(autouse=True)
def isolated_admission_and_watches(
    isolated_desk_stores: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Bind ApprovalAdmission to the same throwaway journal as opportunities/intents.

    Overrides the unit-suite fixture that used a private StaticPool engine —
    FKs and authority loads require one database. Depends on desk_stores so
    OPPORTUNITIES/INTENTS and ADMISSION_RECORDS share one engine binding.
    """
    from sqlalchemy import text

    from database.session import get_sync_engine, session_factory
    from trading import admission_records as adm_mod
    from trading import shadow_outcomes as shadow_mod
    from trading.entry_watch_persistence import (
        configure_entry_watch_persistence,
        persistence_enabled,
    )
    from trading.entry_watches import ENTRY_WATCHES
    from trading.opportunities import OPPORTUNITIES

    engine = OPPORTUNITIES._engine or get_sync_engine()
    store = adm_mod.AdmissionRecordStore(engine=engine)
    shadow = shadow_mod.ShadowOutcomeStore(engine=engine)
    monkeypatch.setattr(adm_mod, "ADMISSION_RECORDS", store)
    monkeypatch.setattr(shadow_mod, "SHADOW_OUTCOMES", shadow)

    SessionLocal = session_factory(engine)
    with SessionLocal() as session:
        session.execute(text("DELETE FROM admission_records"))
        session.execute(text("DELETE FROM shadow_outcomes"))
        session.commit()

    prev_persistence = persistence_enabled()
    configure_entry_watch_persistence(enabled=False)
    ENTRY_WATCHES.clear()
    yield
    ENTRY_WATCHES.clear()
    configure_entry_watch_persistence(enabled=prev_persistence)


@pytest.fixture(autouse=True)
def isolated_default_journal(
    isolated_database: None,
) -> Iterator[None]:
    """Suppress the unit suite's separate unit_journal.db.

    Root `isolated_default_journal` otherwise races this conftest and can
    re-point `get_sync_engine()` at unit_journal after admission was bound to
    integration.db — leaving ApprovalAdmission FKs unreadable at place_order.
    """
    yield


@pytest.fixture(autouse=True)
def isolated_ledger(isolated_database: None) -> Iterator[None]:
    """Override the unit suite's separate in-memory ledger engine.

    Same reason: the integration suite wants one coherent database holding
    opportunities, exits, intents, positions and audit together, because
    reconciliation reads across all of them.
    """
    yield


@pytest.fixture(autouse=True)
def isolated_desk_stores(
    isolated_database: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point OPPORTUNITIES/EXITS/INTENTS at the same throwaway journal file."""
    from database.session import get_sync_engine, init_db
    from trading.exits import EXITS
    from trading.intents import INTENTS
    from trading.ledger import LEDGER
    from trading.opportunities import OPPORTUNITIES

    get_sync_engine.cache_clear()
    engine = get_sync_engine()
    init_db(engine)
    monkeypatch.setattr(EXITS, "_engine", engine)
    monkeypatch.setattr(OPPORTUNITIES, "_engine", engine)
    monkeypatch.setattr(INTENTS, "_engine", engine)
    monkeypatch.setattr(LEDGER, "_engine", engine)
    yield


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """One throwaway journal database per test, shared by every store.

    A file rather than `sqlite://` in memory: the stores open independent
    connections and an in-memory database is private to each one, so the ledger
    would not see what the intent store wrote.
    """
    from database import session as session_mod
    from database.session import get_sync_engine, init_db

    url = f"sqlite:///{tmp_path / 'integration.db'}"
    monkeypatch.setenv("TRAIDO_JOURNAL_DATABASE_URL", url)
    session_mod.get_sync_engine.cache_clear()
    init_db(get_sync_engine())
    yield
    session_mod.get_sync_engine.cache_clear()


@pytest.fixture(autouse=True)
def isolated_kill_switch(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the flag out of the developer's `data/` directory.

    `set_kill_switch` writes a real file at the repository root. A test that
    halts trading and then fails would leave the desk halted afterwards.
    """
    import risk.kill_switch as ks

    monkeypatch.setattr(ks, "FLAG", tmp_path / "kill_switch.on")
    monkeypatch.delenv("REDIS_URL", raising=False)
    yield


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> Iterator[Desk]:
    """The application, wired as production wires it, talking to a fake venue."""
    import api.routes.desk as desk_routes
    from api.main import app

    backend = RecordingBackend()
    broker = DegradableBroker(
        api_key="integration-key",
        api_secret="integration-secret",
        base_url="https://paper-api.example.test",
        transport=backend.transport(),
    )
    market = ScriptedMarketData()

    # Patched at each module's *view* of the outside world, not at the
    # composition root itself. If `build_execution_service` stopped passing
    # `market_data` through, these substitutions would not hide it — the service
    # would still be built without a data port and the liquidity gate would
    # still refuse. That is the regression this whole file exists to catch.
    #
    # That the list has six entries rather than one is a finding, not a
    # convenience: `api/deps.py` is documented as the single composition root,
    # but `review`, `evaluation`, `health`, `trading` and `pipeline` each build
    # their own broker or data port. None of those five can mutate the broker
    # today, so it is a Phase 3 violation rather than a capital risk — but it is
    # the same shape of defect as the liquidity wiring bug, and a future route
    # added next to one of them inherits the habit.
    _substitute_vendors(monkeypatch, broker=broker, market=market)

    # The desk caches its broker snapshot in module globals; a leftover from a
    # previous test would answer before reconciliation ever ran.
    monkeypatch.setattr(desk_routes, "_broker_cache", None)
    monkeypatch.setattr(desk_routes, "_broker_cache_mono", 0.0)
    RECONCILE.reset()

    monkeypatch.delenv("TRAIDO_API_KEY", raising=False)

    # The adapter keeps a four-second, process-wide cache of account reads so
    # that dashboard polling does not rate-limit the vendor. In production the
    # events these tests stage — a fill, a venue-side reduction, a stop being
    # cancelled — are minutes apart, so the cache never stands between them.
    # Here a whole scenario runs inside one TTL window, which would mean every
    # read after the first returns the state from before the test's own setup.
    # Set to zero so the fake venue answers each question honestly; the caching
    # behaviour itself is covered by the adapter's own tests.
    monkeypatch.setattr(alpaca_mod, "_CACHE_TTL_SEC", 0.0)
    alpaca_mod._CACHE.clear()

    # How long the desk waits for a fill is a real production setting (18s in
    # session, 2.5s outside) resolved from the *wall* clock, not the frozen one.
    # Left alone, a partial-fill test would either block for eighteen seconds or
    # change behaviour depending on the hour the suite happens to run.
    from trading import execution as execution_mod

    monkeypatch.setattr(execution_mod, "fill_wait_seconds", lambda **_: 0.25)

    # Macro + sector assessments are required for Final Admission. Integration
    # desk opts in explicitly (unit suite no longer auto-clears them).
    from datetime import UTC
    from datetime import datetime as _dt

    from core.enums import AssessmentKind, DataHealthStatus, MarketRegimeLabel
    from core.schemas import MarketAssessment
    from trading.sector_assessment import SectorMarketAssessment, set_sector_assessment_port
    from trading.sector_classification import classify_symbol
    from trading.sector_policy import SECTOR_ASSESSMENT_VERSION

    async def _assess_market(fred_api_key=None, *, now=None):
        from trading import execution

        evaluated_at = now or execution._utcnow()
        if evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=UTC)
        return MarketAssessment(
            kind=AssessmentKind.MARKET,
            regime=MarketRegimeLabel.RISK_ON,
            score=70,
            risk_posture="risk_on",
            reasons=["integration_cleared_market"],
            evaluated_at=evaluated_at,
            benchmark="SPY",
        )

    class _IntegrationSectorPort:
        async def assess(self, symbol, *, market_data=None, symbol_bars=None, now=None):
            evaluated_at = now or _dt.now(UTC)
            if evaluated_at.tzinfo is None:
                evaluated_at = evaluated_at.replace(tzinfo=UTC)
            cls = classify_symbol(symbol)
            if cls.benchmark is None:
                return SectorMarketAssessment(
                    symbol=cls.symbol,
                    evaluated_at=evaluated_at,
                    data_status=DataHealthStatus.UNHEALTHY,
                    tradable_long=None,
                    reason_codes=("SECTOR_METADATA_MISSING",),
                    assessment_version=SECTOR_ASSESSMENT_VERSION,
                )
            return SectorMarketAssessment(
                symbol=cls.symbol,
                sector=cls.sector,
                industry=cls.industry,
                benchmark=cls.benchmark,
                benchmark_bars_count=120,
                benchmark_last_bar_ts=evaluated_at,
                evaluated_at=evaluated_at,
                data_status=DataHealthStatus.HEALTHY,
                sector_regime=MarketRegimeLabel.BULLISH,
                tradable_long=True,
                reason_codes=("SECTOR_BENCHMARK_OK", "INTEGRATION_FIXTURE"),
                assessment_version=SECTOR_ASSESSMENT_VERSION,
                classification_provider=cls.classification_provider,
                classification_version=cls.classification_version,
            )

    monkeypatch.setattr("agents.market.agent.assess_market", _assess_market)
    set_sector_assessment_port(_IntegrationSectorPort())

    # No `with`: the lifespan would start the scanner loop, and a background
    # walker scanning the universe is not part of any assertion here.
    client = TestClient(app)
    stand = Desk(client=client, broker=broker, backend=backend, market=market)

    # Production reaches this state within one tick of startup, and new exposure
    # is refused until it does (`RECONCILIATION_NEVER_RAN`). Without it every
    # test that approves anything would be testing the cold-start refusal.
    stand.reconcile_now()

    yield stand
    set_sector_assessment_port(None)
    client.close()


@pytest.fixture
def open_market(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Mid-session, so the RTH gate passes for tests that are not about RTH."""
    from tests.conftest import RTH_INSTANT

    return RTH_INSTANT.astimezone(UTC)
