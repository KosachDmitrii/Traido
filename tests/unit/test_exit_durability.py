"""
Stage 7.1: the exit side must be as durable as the entry side.

Every test here describes a way the old exit path could lose money or lose
truth: a second SELL after a lost reply, a partial exit that closed the whole
position on paper, a remainder left without a stop, an emergency close fired
twice by two workers.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from broker.interface import BrokerRejection, BrokerUnreachable
from core.audit import InMemoryAudit
from core.enums import (
    IntentPurpose,
    IntentStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeAction,
    TradingMode,
    UserDecision,
)
from core.schemas import (
    ExitProposal,
    OrderRecord,
    OrderRequest,
    PortfolioSnapshot,
    Position,
    PositionStatus,
    TradeCandidate,
)
from risk.kill_switch import set_kill_switch
from trading.execution import ExecutionService
from trading.exits import EXIT_AWAITING, EXIT_SOLD, MemoryExitStore
from trading.intents import MemoryOrderIntentStore
from trading.ledger import PositionLedger
from trading.opportunities import MemoryOpportunityStore

pytestmark = pytest.mark.asyncio


# ── Doubles ──────────────────────────────────────────────────────────────────


class _ExitBroker:
    """Broker with the exit failure modes worth designing against.

    `fill_ratio` drives partial exits; `lose_reply` simulates an accepted order
    whose acknowledgement never came back, which is the case that used to
    produce a second sell.
    """

    environment = "paper"

    def __init__(
        self,
        *,
        held: Decimal = Decimal(100),
        fill_ratio: Decimal = Decimal(1),
        lose_reply: bool = False,
        reject_sells: bool = False,
        stop_placement_fails: bool = False,
    ) -> None:
        self.held = held
        self.fill_ratio = fill_ratio
        self.lose_reply = lose_reply
        self.reject_sells = reject_sells
        self.stop_placement_fails = stop_placement_fails
        self.orders: list[OrderRequest] = []
        self.records: dict[str, OrderRecord] = {}
        self.by_client: dict[str, OrderRecord] = {}
        self.canceled: list[str] = []

    @property
    def sells(self) -> list[OrderRequest]:
        return [o for o in self.orders if o.side == OrderSide.SELL]

    @property
    def market_sells(self) -> list[OrderRequest]:
        return [o for o in self.sells if o.order_type == OrderType.MARKET]

    @property
    def stops(self) -> list[OrderRequest]:
        return [o for o in self.sells if o.order_type == OrderType.STOP]

    async def get_portfolio(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            equity=Decimal(100_000),
            cash=Decimal(50_000),
            buying_power=Decimal(50_000),
            open_exposure=Decimal(0),
            open_positions=1 if self.held > 0 else 0,
            day_pnl=Decimal(0),
            week_pnl=Decimal(0),
            drawdown_pct=0.0,
            kill_switch=False,
        )

    async def list_positions(self) -> list[Position]:
        if self.held <= 0:
            return []
        return [
            Position(
                id=uuid4(),
                symbol="AAPL",
                qty=self.held,
                avg_entry=Decimal(100),
                status=PositionStatus.OPEN,
                opened_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )
        ]

    async def list_open_orders(self) -> list[OrderRecord]:
        return [
            r
            for r in self.records.values()
            if r.status in {OrderStatus.ACCEPTED, OrderStatus.SUBMITTED, OrderStatus.PARTIAL}
        ]

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        self.orders.append(request)
        if request.order_type == OrderType.STOP and self.stop_placement_fails:
            raise BrokerRejection("stop rejected")
        if request.side == OrderSide.SELL and self.reject_sells:
            raise BrokerRejection("sell rejected")

        oid = f"broker-{len(self.orders)}"
        if request.order_type == OrderType.STOP:
            record = OrderRecord(
                id=uuid4(),
                client_order_id=request.client_order_id,
                broker_order_id=oid,
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                status=OrderStatus.ACCEPTED,
                stop_price=request.stop_price,
            )
        else:
            filled = (request.qty * self.fill_ratio).quantize(Decimal(1))
            record = OrderRecord(
                id=uuid4(),
                client_order_id=request.client_order_id,
                broker_order_id=oid,
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                status=(OrderStatus.FILLED if filled >= request.qty else OrderStatus.PARTIAL),
                filled_qty=filled or None,
                filled_avg_price=Decimal(110),
            )
            if request.side == OrderSide.SELL:
                self.held -= filled

        self.records[oid] = record
        self.by_client[request.client_order_id] = record
        if self.lose_reply and request.side == OrderSide.SELL:
            # The broker has the order. We never learn its id.
            raise BrokerUnreachable("gateway timed out after accepting the order")
        return record

    async def cancel_order(self, broker_order_id: str) -> OrderRecord:
        self.canceled.append(broker_order_id)
        record = self.records.get(broker_order_id)
        if record is None:
            raise RuntimeError("order not found")
        if record.status is not OrderStatus.FILLED:
            self.records[broker_order_id] = record.model_copy(
                update={"status": OrderStatus.CANCELED}
            )
        return self.records[broker_order_id]

    async def get_order(self, broker_order_id: str) -> OrderRecord:
        record = self.records.get(broker_order_id)
        if record is None:
            raise BrokerUnreachable(f"unknown order {broker_order_id}")
        return record

    async def find_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        return self.by_client.get(client_order_id)


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(120),
        risk_reward=4.0,
        reasons=["exit durability fixture"],
        strategy_version="test-v1",
    )


def _seed_position(ledger: PositionLedger, *, qty: Decimal, stop_order_id: str | None) -> UUID:
    store = MemoryOpportunityStore()
    from risk.risk_engine import RiskEngine

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
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    row = ledger.open_from_opportunity(
        opp,
        qty=qty,
        broker_entry_order_id="entry-1",
        fill_price=Decimal(100),
        stop_order_id=stop_order_id,
    )
    return row.id


def _proposal(position_id: UUID) -> ExitProposal:
    return ExitProposal(
        position_id=position_id,
        symbol="AAPL",
        entry=Decimal(100),
        current=Decimal(110),
        pnl_pct=10.0,
        reasons=["Target reached"],
        recommendation=UserDecision.SELL,
        confidence=0.9,
    )


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> PositionLedger:
    """A ledger on its own in-memory database, wired in where the code looks."""
    from sqlalchemy import create_engine

    import trading.execution
    import trading.intents
    import trading.ledger
    import trading.reconcile

    engine = create_engine("sqlite://", future=True)
    isolated = PositionLedger(engine)
    monkeypatch.setattr(trading.ledger, "LEDGER", isolated)
    monkeypatch.setattr(trading.execution, "LEDGER", isolated, raising=False)
    monkeypatch.setattr(trading.reconcile, "LEDGER", isolated)
    monkeypatch.setattr(trading.intents, "LEDGER", isolated, raising=False)
    return isolated


def _service(
    broker: _ExitBroker,
    exits: MemoryExitStore,
    intents: MemoryOrderIntentStore,
    audit: InMemoryAudit,
) -> ExecutionService:
    set_kill_switch(False)
    return ExecutionService(
        broker=broker,  # type: ignore[arg-type]
        audit=audit,
        store=MemoryOpportunityStore(),
        exit_store=exits,
        intents=intents,
        fill_timeout_sec=1.0,
    )


# ── Durable exit intent ──────────────────────────────────────────────────────


async def test_an_exit_is_persisted_before_the_broker_is_contacted(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker()
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))

    await _service(broker, exits, intents, InMemoryAudit()).decide_exit(item.id, UserDecision.SELL)

    recorded = intents.list_by_key_prefix(f"exit:{position_id}:")
    assert len(recorded) == 1
    assert recorded[0].purpose is IntentPurpose.EXIT
    assert recorded[0].status is IntentStatus.FILLED
    assert recorded[0].client_order_id, "the handle recovery needs must be persisted"


async def test_a_retry_after_a_lost_sell_reply_does_not_sell_twice(
    ledger: PositionLedger,
) -> None:
    """The core exit-side duplicate. The broker took the order; we never heard."""
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker(lose_reply=True)
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))
    audit = InMemoryAudit()

    with pytest.raises(RuntimeError, match="EXIT_STATE_UNKNOWN"):
        await _service(broker, exits, intents, audit).decide_exit(item.id, UserDecision.SELL)

    assert len(broker.market_sells) == 1
    intent = intents.list_by_key_prefix(f"exit:{position_id}:")[0]
    assert intent.status is IntentStatus.UNKNOWN

    # A fresh process picks the card back up and retries.
    broker.lose_reply = False
    exits.update(exits.get(item.id).model_copy(update={"status": EXIT_AWAITING}))
    result = await _service(broker, exits, intents, audit).decide_exit(item.id, UserDecision.SELL)

    assert len(broker.market_sells) == 1, "the retry must adopt the first sell, not add one"
    assert result.status == EXIT_SOLD
    assert any(e["event_type"] == "DuplicateOrderPrevented" for e in audit.events)


async def test_pressing_sell_twice_produces_one_broker_order(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker()
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))
    service = _service(broker, exits, intents, InMemoryAudit())

    first = await service.decide_exit(item.id, UserDecision.SELL)
    second = await service.decide_exit(item.id, UserDecision.SELL)

    assert first.status == second.status == EXIT_SOLD
    assert len(broker.market_sells) == 1


# ── Partial exits and the ledger ─────────────────────────────────────────────


async def test_a_partial_exit_leaves_the_remainder_open(ledger: PositionLedger) -> None:
    """The old path closed the whole position on a 30-of-100 fill."""
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker(fill_ratio=Decimal("0.3"))
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))

    result = await _service(broker, exits, intents, InMemoryAudit()).decide_exit(
        item.id, UserDecision.SELL
    )

    row = ledger.get(position_id)
    assert row is not None
    assert row.status == "open", "70 shares are still ours"
    assert Decimal(str(row.qty)) == Decimal(30 * 0 + 70)
    assert result.status == EXIT_AWAITING, "the rest is still sellable"


async def test_a_partial_exit_resizes_protection_to_the_remainder(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker(fill_ratio=Decimal("0.3"))
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    item = exits.upsert(_proposal(position_id))

    await _service(broker, exits, intents, audit).decide_exit(item.id, UserDecision.SELL)

    stops = broker.stops
    assert stops, "the remainder must not be left naked"
    assert stops[-1].qty == Decimal(70)
    resized = next(e for e in audit.events if e["event_type"] == "ProtectionResized")
    assert Decimal(resized["payload"]["remaining_qty"]) == Decimal(70)


async def test_protection_never_exceeds_the_remaining_position(
    ledger: PositionLedger,
) -> None:
    """A stop for more shares than we hold would open a short."""
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker(fill_ratio=Decimal("0.6"))
    exits = MemoryExitStore()
    item = exits.upsert(_proposal(position_id))

    await _service(broker, exits, MemoryOrderIntentStore(), InMemoryAudit()).decide_exit(
        item.id, UserDecision.SELL
    )

    row = ledger.get(position_id)
    assert row is not None
    for stop in broker.stops:
        assert stop.qty <= Decimal(str(row.qty))


async def test_a_full_exit_closes_the_position_exactly_once(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id="stop-1")
    broker = _ExitBroker()
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))
    service = _service(broker, exits, intents, InMemoryAudit())

    await service.decide_exit(item.id, UserDecision.SELL)
    await service.decide_exit(item.id, UserDecision.SELL)

    assert ledger.get_open("AAPL") == []
    assert len(ledger.list_closed_journal()) == 1


async def test_staged_exits_journal_a_blended_price(ledger: PositionLedger) -> None:
    """Two legs at different prices must not journal only the last one."""
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id=None)

    ledger.apply_exit_fill(
        symbol="AAPL",
        position_id=position_id,
        filled_qty=Decimal(50),
        exit_price=Decimal(110),
        exit_reasons=["leg 1"],
    )
    final = ledger.apply_exit_fill(
        symbol="AAPL",
        position_id=position_id,
        filled_qty=Decimal(50),
        exit_price=Decimal(130),
        exit_reasons=["leg 2"],
    )

    assert final.closed
    assert final.journal is not None
    assert Decimal(str(final.journal.exit)) == Decimal(120)
    assert Decimal(str(final.journal.qty)) == Decimal(100)


async def test_the_ledger_refuses_to_close_a_position_twice(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(10), stop_order_id=None)
    kwargs = {
        "symbol": "AAPL",
        "position_id": position_id,
        "filled_qty": Decimal(10),
        "exit_price": Decimal(110),
        "exit_reasons": ["close"],
    }

    first = ledger.apply_exit_fill(**kwargs)  # type: ignore[arg-type]
    second = ledger.apply_exit_fill(**kwargs)  # type: ignore[arg-type]

    assert first.closed and first.journal is not None
    assert not second.found, "the second attempt must find nothing to close"
    assert len(ledger.list_closed_journal()) == 1


# ── Emergency close ──────────────────────────────────────────────────────────


async def test_two_workers_triggering_an_emergency_close_send_one_order(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(50), stop_order_id=None)
    broker = _ExitBroker(held=Decimal(50))
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    service = _service(broker, MemoryExitStore(), intents, audit)

    first = await service._emergency_flatten(
        symbol="AAPL",
        qty=Decimal(50),
        pipeline_run_id=None,
        reason="stop_failed:boom",
        position_id=position_id,
    )
    second = await service._emergency_flatten(
        symbol="AAPL",
        qty=Decimal(50),
        pipeline_run_id=None,
        reason="stop_failed:boom",
        position_id=position_id,
    )

    assert first is True
    assert second is True, "the second trigger resolves against the first order"
    assert len(broker.market_sells) == 1


async def test_an_emergency_close_is_persisted_as_an_intent(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(50), stop_order_id=None)
    broker = _ExitBroker(held=Decimal(50))
    intents = MemoryOrderIntentStore()
    service = _service(broker, MemoryExitStore(), intents, InMemoryAudit())

    await service._emergency_flatten(
        symbol="AAPL",
        qty=Decimal(50),
        pipeline_run_id=None,
        reason="stop_failed:boom",
        position_id=position_id,
    )

    recorded = intents.list_by_key_prefix(f"emergency_exit:{position_id}:")
    assert len(recorded) == 1
    assert recorded[0].purpose is IntentPurpose.EMERGENCY_EXIT
    assert recorded[0].status is IntentStatus.FILLED


async def test_an_unconfirmed_emergency_close_is_unknown_not_safe(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(50), stop_order_id=None)
    broker = _ExitBroker(held=Decimal(50), lose_reply=True)
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    service = _service(broker, MemoryExitStore(), intents, audit)

    flat = await service._emergency_flatten(
        symbol="AAPL",
        qty=Decimal(50),
        pipeline_run_id=None,
        reason="stop_failed:boom",
        position_id=position_id,
    )

    assert flat is False
    intent = intents.list_by_key_prefix(f"emergency_exit:{position_id}:")[0]
    assert intent.status is IntentStatus.UNKNOWN
    critical = next(e for e in audit.events if e["event_type"] == "EmergencyExitUnknown")
    assert critical["payload"]["severity"] == "critical"


async def test_a_partial_emergency_close_does_not_report_the_position_as_safe(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id=None)
    broker = _ExitBroker(held=Decimal(100), fill_ratio=Decimal("0.4"))
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    service = _service(broker, MemoryExitStore(), intents, audit)

    flat = await service._emergency_flatten(
        symbol="AAPL",
        qty=Decimal(100),
        pipeline_run_id=None,
        reason="stop_failed:boom",
        position_id=position_id,
    )

    assert flat is False, "60 shares are still unprotected"
    row = ledger.get(position_id)
    assert row is not None and Decimal(str(row.qty)) == Decimal(60)
    assert any(e["event_type"] == "EmergencyExitUnknown" for e in audit.events)


# ── UNKNOWN blocking ─────────────────────────────────────────────────────────


async def test_an_unresolved_emergency_close_blocks_a_discretionary_exit(
    ledger: PositionLedger,
) -> None:
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id=None)
    broker = _ExitBroker(held=Decimal(100), lose_reply=True)
    intents = MemoryOrderIntentStore()
    exits = MemoryExitStore()
    audit = InMemoryAudit()
    service = _service(broker, exits, intents, audit)

    await service._emergency_flatten(
        symbol="AAPL",
        qty=Decimal(100),
        pipeline_run_id=None,
        reason="stop_failed:boom",
        position_id=position_id,
    )
    broker.lose_reply = False
    item = exits.upsert(_proposal(position_id))

    with pytest.raises(RuntimeError, match="EMERGENCY_EXIT_IN_FLIGHT"):
        await service.decide_exit(item.id, UserDecision.SELL)

    assert len(broker.market_sells) == 1
    assert any(e["event_type"] == "ExitBlockedByUnresolvedState" for e in audit.events)


async def test_a_restart_during_an_exit_recovers_through_reconciliation(
    ledger: PositionLedger,
) -> None:
    """No shared memory survives; only the intent store and the broker do."""
    from trading.reconcile import reconcile_order_intents

    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id=None)
    broker = _ExitBroker(lose_reply=True)
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))

    with pytest.raises(RuntimeError, match="EXIT_STATE_UNKNOWN"):
        await _service(broker, exits, intents, InMemoryAudit()).decide_exit(
            item.id, UserDecision.SELL
        )

    # Process dies here. A new one starts with nothing but the durable record.
    surviving = MemoryOrderIntentStore()
    for intent in intents.list_unresolved():
        surviving.create_or_get(intent)
    audit = InMemoryAudit()

    await reconcile_order_intents(broker, surviving, audit)  # type: ignore[arg-type]

    assert len(broker.market_sells) == 1, "recovery reads, it does not re-send"
    assert ledger.get_open("AAPL") == [], "the sale that happened is on the books"
    assert surviving.list_unresolved() == []


async def test_a_definite_rejection_releases_the_card(ledger: PositionLedger) -> None:
    """Unlike an ambiguous failure: the broker answered, so no order exists."""
    position_id = _seed_position(ledger, qty=Decimal(100), stop_order_id=None)
    broker = _ExitBroker(reject_sells=True)
    exits = MemoryExitStore()
    intents = MemoryOrderIntentStore()
    item = exits.upsert(_proposal(position_id))

    with pytest.raises(RuntimeError, match="EXIT_ORDER_REJECTED"):
        await _service(broker, exits, intents, InMemoryAudit()).decide_exit(
            item.id, UserDecision.SELL
        )

    assert exits.get(item.id).status == EXIT_AWAITING
    assert intents.list_by_key_prefix(f"exit:{position_id}:")[0].status is IntentStatus.REJECTED
