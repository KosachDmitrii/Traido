"""
The gates must actually stop an order, not merely produce a verdict.

A gate that runs early and gets ignored later is decoration. These tests assert
at the only place that matters: whether the broker saw an order.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    OpportunityStatus,
    Timeframe,
    TradeAction,
    TradingMode,
    UserDecision,
)
from core.schemas import Bar, PortfolioSnapshot, Quote, TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.gates import LiquidityPolicy
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]


ET = ZoneInfo("America/New_York")


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal("100.00"),
        stop=Decimal("95.00"),
        target=Decimal("112.00"),
        risk_reward=2.4,
        reasons=["test setup"],
        strategy_version="test-v1",
    )


SESSION = datetime(2026, 3, 10, 11, 0, tzinfo=ET)
SATURDAY = datetime(2026, 3, 14, 11, 0, tzinfo=ET)
"""Outside any session — only reachable with the RTH gate deliberately off."""


class _Bars:
    """MarketDataPort stand-in serving one fixed history, and a quote if asked."""

    def __init__(
        self,
        *,
        volume: float,
        quote_age_sec: float | None = 0.0,
        now: datetime = SESSION,
    ) -> None:
        self.volume = volume
        self.quote_age_sec = quote_age_sec
        self.now = now
        """The instant the quote is measured against — the service's clock, not
        the wall clock, so a test that moves the session moves the quote too."""

    async def get_quote(self, symbol: str) -> Quote | None:
        if self.quote_age_sec is None:
            return None
        return Quote(
            symbol=symbol,
            bid=Decimal("99.99"),
            ask=Decimal("100.01"),
            ts=self.now - timedelta(seconds=self.quote_age_sec),
            source="synthetic",
        )

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        # Anchored to the requested window, as a real feed is. Returning a fixed
        # date regardless of `end` made every series permanently stale, which
        # only became visible once a freshness gate started reading it.
        base = end - timedelta(days=59)
        return [
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=base + timedelta(days=i),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=self.volume,
                source="synthetic",
            )
            for i in range(60)
        ]

    async def get_last_price(self, symbol: str) -> float:
        return 100.0


async def _setup() -> tuple[MockPaperBroker, MemoryOpportunityStore, object]:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    return broker, store, store.create(_candidate(), risk, TradingMode.CONFIRMATION)


# ── RTH ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "moment"),
    [
        ("premarket", datetime(2026, 3, 10, 8, 0, tzinfo=ET)),
        ("after_hours", datetime(2026, 3, 10, 17, 0, tzinfo=ET)),
        ("weekend", datetime(2026, 3, 14, 11, 0, tzinfo=ET)),
        ("holiday", datetime(2026, 12, 25, 11, 0, tzinfo=ET)),
    ],
)
async def test_no_entry_reaches_the_broker_outside_regular_hours(
    label: str, moment: datetime
) -> None:
    broker, store, opp = await _setup()
    audit = InMemoryAudit()
    service = ExecutionService(
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        clock=lambda: moment,
    )

    with pytest.raises(RuntimeError, match="RTH_GATE_REJECTED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert broker.orders == [], label
    assert any(e["event_type"] == "RTHGateRejected" for e in audit.events)
    # The card returns to the queue: the setup is fine, the timing is not.
    assert store.get(opp.id).status is OpportunityStatus.AWAITING_CONFIRMATION


async def test_an_entry_during_the_regular_session_proceeds() -> None:
    broker, store, opp = await _setup()
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0),
        clock=lambda: datetime(2026, 3, 10, 11, 0, tzinfo=ET),
    )

    result = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )

    assert result.status is OpportunityStatus.EXECUTED
    assert broker.orders


async def test_the_rth_gate_can_be_disabled_for_environments_that_trade_extended() -> None:
    """Explicit configuration, not an accident of which module imported which."""
    broker, store, opp = await _setup()
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        require_rth=False,
        market_data=_Bars(volume=5_000_000.0, now=SATURDAY),
        clock=lambda: SATURDAY,
    )

    assert (
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    ).status is (OpportunityStatus.EXECUTED)


# ── Liquidity ────────────────────────────────────────────────────────────────


async def test_an_illiquid_symbol_never_reaches_the_broker() -> None:
    broker, store, opp = await _setup()
    audit = InMemoryAudit()
    service = ExecutionService(
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=500.0),
        clock=lambda: datetime(2026, 3, 10, 11, 0, tzinfo=ET),
    )

    with pytest.raises(RuntimeError, match="LIQUIDITY_GATE_REJECTED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert broker.orders == []
    rejection = next(e for e in audit.events if e["event_type"] == "LiquidityGateRejected")
    assert "INSUFFICIENT_AVG_DOLLAR_VOLUME" in rejection["payload"]["reasons"]


async def test_a_liquid_symbol_passes_the_gate() -> None:
    broker, store, opp = await _setup()
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0),
        clock=lambda: datetime(2026, 3, 10, 11, 0, tzinfo=ET),
    )

    assert (
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    ).status is (OpportunityStatus.EXECUTED)


async def test_no_live_quote_means_no_entry() -> None:
    """A spread we never observed is not a spread that passed."""
    broker, store, opp = await _setup()
    audit = InMemoryAudit()
    service = ExecutionService(
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0, quote_age_sec=None),
        clock=lambda: SESSION,
    )

    with pytest.raises(RuntimeError, match="LIQUIDITY_GATE_REJECTED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert broker.orders == []
    rejection = next(e for e in audit.events if e["event_type"] == "LiquidityGateRejected")
    assert "LIVE_QUOTE_REQUIRED" in rejection["payload"]["reasons"]


async def test_a_stale_quote_does_not_count_as_a_live_spread_check() -> None:
    broker, store, opp = await _setup()
    audit = InMemoryAudit()
    service = ExecutionService(
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0, quote_age_sec=600.0),
        clock=lambda: SESSION,
    )

    with pytest.raises(RuntimeError, match="LIQUIDITY_GATE_REJECTED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    rejection = next(e for e in audit.events if e["event_type"] == "LiquidityGateRejected")
    assert "QUOTE_STALE" in rejection["payload"]["reasons"]
    assert broker.orders == []


async def test_unavailable_market_data_blocks_rather_than_waves_through() -> None:
    """A gate that cannot measure must fail closed."""

    class _Broken:
        async def get_bars(self, *args: object, **kwargs: object) -> list[Bar]:
            raise RuntimeError("vendor outage")

        async def get_last_price(self, symbol: str) -> float:
            raise RuntimeError("vendor outage")

    broker, store, opp = await _setup()
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Broken(),  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 3, 10, 11, 0, tzinfo=ET),
    )

    with pytest.raises(RuntimeError, match="LIQUIDITY_GATE_REJECTED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.orders == []


async def test_the_price_floor_is_enforced_at_execution_time() -> None:
    broker, store, opp = await _setup()
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0),
        liquidity_policy=LiquidityPolicy(min_price=Decimal(500)),
        clock=lambda: datetime(2026, 3, 10, 11, 0, tzinfo=ET),
    )

    with pytest.raises(RuntimeError, match="PRICE_BELOW_FLOOR"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.orders == []


# ── One position per symbol ──────────────────────────────────────────────────


def _seed_open_position(store: MemoryOpportunityStore, qty: Decimal) -> object:
    """Put a position on the book the way a completed entry would."""
    from trading.ledger import LEDGER

    risk = RiskEngine().evaluate(
        _candidate(),
        PortfolioSnapshot(
            equity=Decimal(100_000),
            cash=Decimal(100_000),
            buying_power=Decimal(100_000),
            open_exposure=Decimal(0),
            open_positions=0,
            day_pnl=Decimal(0),
            week_pnl=Decimal(0),
            drawdown_pct=0.0,
            kill_switch=False,
        ),
    )
    held = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    return LEDGER.open_from_opportunity(
        held,
        qty=qty,
        broker_entry_order_id="entry-seed",
        fill_price=Decimal(100),
        stop_order_id="stop-seed",
    )


async def test_a_second_entry_is_refused_while_the_symbol_is_already_held() -> None:
    """Two rows for one symbol can never agree with a broker's net position.

    The broker reports one AAPL position, so a book holding two of them has no
    row that reconciliation can call correct — it compares both against the same
    number and blocks the symbol. Refusing here costs nothing, because no order
    has been sent yet.
    """
    from trading.ledger import LEDGER

    broker, store, opp = await _setup()
    _seed_open_position(store, Decimal(50))
    audit = InMemoryAudit()
    service = ExecutionService(
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0),
        clock=lambda: SESSION,
    )

    with pytest.raises(RuntimeError, match="POSITION_ALREADY_OPEN"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert broker.orders == [], "nothing may reach the broker"
    assert any(e["event_type"] == "EntryBlockedByOpenPosition" for e in audit.events)
    assert len(LEDGER.get_open("AAPL")) == 1
    # The setup itself is sound, so the card goes back in the queue.
    assert store.get(opp.id).status is OpportunityStatus.AWAITING_CONFIRMATION


async def test_the_ledger_itself_refuses_a_second_open_row() -> None:
    """The backstop, for any path that does not go through the gate."""
    from trading.ledger import LEDGER, DuplicateOpenPosition

    store = MemoryOpportunityStore()
    _seed_open_position(store, Decimal(50))

    with pytest.raises(DuplicateOpenPosition, match="AAPL"):
        _seed_open_position(store, Decimal(50))

    assert len(LEDGER.get_open("AAPL")) == 1, "the desk must never show the symbol twice"


async def test_the_symbol_can_be_re_entered_once_the_position_is_closed() -> None:
    """The rule is one *open* position, not one position ever."""
    from trading.ledger import LEDGER

    broker, store, opp = await _setup()
    _seed_open_position(store, Decimal(50))
    LEDGER.apply_exit_fill(
        symbol="AAPL",
        filled_qty=Decimal(50),
        exit_price=Decimal(110),
        exit_reasons=["closed for the test"],
    )
    assert LEDGER.get_open("AAPL") == []

    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
        market_data=_Bars(volume=5_000_000.0),
        clock=lambda: SESSION,
    )

    assert (
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    ).status is (OpportunityStatus.EXECUTED)
