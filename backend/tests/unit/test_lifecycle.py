"""Position lifecycle — fill gate, stop hard-fail flatten, journal at fill."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from broker.interface import BrokerRejection, BrokerUnreachable
from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    OpportunityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
    UserDecision,
)
from core.schemas import OrderRecord, OrderRequest, TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, admission_ready_candidate, liquid_market_data
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.opportunities import MemoryOpportunityStore

pytestmark = pytest.mark.usefixtures("capital_path_ready")


def _candidate() -> TradeCandidate:
    return admission_ready_candidate(strategy_version="strategy_confluence@0.2.0")


@pytest.mark.asyncio
async def test_approve_opens_ledger_at_fill_price() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    audit = InMemoryAudit()
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
    )
    result = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    assert result.status == OpportunityStatus.EXECUTED
    assert any(e["event_type"] == "FillReceived" for e in audit.events)
    assert any(e["event_type"] == "PositionOpened" for e in audit.events)
    # Protective stop resting
    assert any(o.order_type.value == "stop" for o in broker.orders)


@pytest.mark.asyncio
async def test_stop_failure_flattens_and_discards(monkeypatch) -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    real_place = broker.place_order

    async def flaky_place(req):
        if req.order_type.value == "stop":
            raise RuntimeError("simulated stop reject")
        return await real_place(req)

    monkeypatch.setattr(broker, "place_order", flaky_place)
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )
    with pytest.raises(RuntimeError, match="STOP_FAILED_FLATTENED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    updated = store.get(opp.id)
    assert updated is not None
    assert updated.status == OpportunityStatus.DISCARDED
    # Flatten should leave no long (or flat after emergency sell)
    assert all(p.symbol != "AAPL" for p in broker.positions) or len(broker.positions) == 0


@pytest.mark.asyncio
async def test_entry_order_reject_releases_to_awaiting(monkeypatch) -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    async def reject(_req):
        raise BrokerRejection("422 sub-penny increment")

    monkeypatch.setattr(broker, "place_order", reject)
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )
    with pytest.raises(RuntimeError, match="ENTRY_ORDER_REJECTED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    updated = store.get(opp.id)
    assert updated is not None
    assert updated.status == OpportunityStatus.AWAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_ambiguous_submit_failure_does_not_release_the_card(monkeypatch) -> None:
    """A refusal frees the card; silence must not.

    When the broker answers "no" we know no order exists and the user may retry.
    When it does not answer at all, an order may be live, so releasing the card
    would invite a second entry into an unknown position.
    """
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    audit = InMemoryAudit()

    async def vanish(_req):
        raise BrokerUnreachable("connection reset before response")

    monkeypatch.setattr(broker, "place_order", vanish)
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
    )

    with pytest.raises(RuntimeError, match="ENTRY_STATE_UNKNOWN"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    updated = store.get(opp.id)
    assert updated is not None
    assert updated.status != OpportunityStatus.AWAITING_CONFIRMATION
    assert any(e["event_type"] == "EntryStateUnknown" for e in audit.events)
    assert "AAPL" in service.intents.unresolved_symbols()


class _NoFillBroker(MockPaperBroker):
    """Entry limit rests unfilled instead of the mock's instant fill."""

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        if request.side == OrderSide.BUY:
            record = OrderRecord(
                id=uuid4(),
                client_order_id=request.client_order_id,
                broker_order_id=str(uuid4()),
                symbol=request.symbol.upper(),
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                status=OrderStatus.ACCEPTED,
                limit_price=request.limit_price,
                filled_avg_price=None,
                filled_qty=Decimal(0),
            )
            self.orders.append(record)
            return record
        return await super().place_order(request)


class _PartialFillBroker(MockPaperBroker):
    """Entry buys `filled` shares, then stalls with the rest outstanding."""

    def __init__(self, filled: Decimal) -> None:
        super().__init__()
        self.filled = filled

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        if request.side == OrderSide.BUY:
            # Acquire only part of the size through the mock's own accounting,
            # then report the order as still partially outstanding.
            done = await super().place_order(request.model_copy(update={"qty": self.filled}))
            partial = done.model_copy(update={"qty": request.qty, "status": OrderStatus.PARTIAL})
            self.orders[-1] = partial
            return partial
        return await super().place_order(request)


async def _timeout(_broker, _oid, timeout_sec=45.0):  # type: ignore[no-untyped-def]
    raise RuntimeError("FILL_TIMEOUT")


def _service(broker, store, audit=None) -> ExecutionService:  # type: ignore[no-untyped-def]
    return ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=audit or InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        fill_timeout_sec=0.01,
    )


@pytest.mark.asyncio
async def test_entry_fill_timeout_releases_to_awaiting(monkeypatch) -> None:
    """Nothing filled: the card goes back to the queue and no position exists."""
    set_kill_switch(False)
    broker = _NoFillBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    monkeypatch.setattr("trading.execution.wait_for_fill", _timeout)
    with pytest.raises(RuntimeError, match="ENTRY_FILL_FAILED"):
        await _service(broker, store).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    updated = store.get(opp.id)
    assert updated is not None
    assert updated.status == OpportunityStatus.AWAITING_CONFIRMATION
    assert broker.positions == []


@pytest.mark.asyncio
async def test_partial_fill_on_timeout_is_protected_not_abandoned(monkeypatch) -> None:
    """Cancelling a partial entry leaves real shares — they must get a stop.

    The old behaviour returned the card to the queue and walked away, leaving
    an unhedged long that only reconciliation would notice.
    """
    set_kill_switch(False)
    broker = _PartialFillBroker(filled=Decimal(4))
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    audit = InMemoryAudit()

    monkeypatch.setattr("trading.execution.wait_for_fill", _timeout)
    result = await _service(broker, store, audit).decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )

    assert result.status == OpportunityStatus.EXECUTED
    assert any(e["event_type"] == "EntryPartiallyFilled" for e in audit.events)

    held = next(p for p in broker.positions if p.symbol == "AAPL")
    assert held.qty == Decimal(4)

    stops = [o for o in broker.orders if o.order_type == OrderType.STOP]
    assert len(stops) == 1
    assert stops[0].qty == Decimal(4), "stop must cover exactly the shares we own"


@pytest.mark.asyncio
async def test_partial_fill_with_failing_stop_is_flattened(monkeypatch) -> None:
    """The naked-long guard still applies to a partially filled entry."""
    set_kill_switch(False)
    broker = _PartialFillBroker(filled=Decimal(4))
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    real_place = broker.place_order

    async def flaky(req):  # type: ignore[no-untyped-def]
        if req.order_type == OrderType.STOP:
            raise RuntimeError("simulated stop reject")
        return await real_place(req)

    monkeypatch.setattr("trading.execution.wait_for_fill", _timeout)
    monkeypatch.setattr(broker, "place_order", flaky)

    with pytest.raises(RuntimeError, match="STOP_FAILED_FLATTENED"):
        await _service(broker, store).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert all(p.symbol != "AAPL" for p in broker.positions)


@pytest.mark.asyncio
async def test_unreadable_entry_after_cancel_is_recorded(monkeypatch) -> None:
    """If the broker cannot tell us what filled, say so loudly."""
    set_kill_switch(False)
    broker = _NoFillBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    audit = InMemoryAudit()

    async def blind(_oid):  # type: ignore[no-untyped-def]
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr("trading.execution.wait_for_fill", _timeout)
    monkeypatch.setattr(broker, "get_order", blind)

    service = _service(broker, store, audit)
    with pytest.raises(RuntimeError, match="ENTRY_STATE_UNKNOWN"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert any(e["event_type"] == "EntryStateUnknown" for e in audit.events)
    # An unreadable entry may have filled, so the symbol stays barred.
    assert "AAPL" in service.intents.unresolved_symbols()
    assert store.get(opp.id).status != OpportunityStatus.AWAITING_CONFIRMATION


def test_wait_for_fill_does_not_treat_partial_as_done() -> None:
    """The root cause: PARTIAL is not a terminal success state."""
    from trading.fills import TERMINAL_OK

    assert OrderStatus.PARTIAL not in TERMINAL_OK
