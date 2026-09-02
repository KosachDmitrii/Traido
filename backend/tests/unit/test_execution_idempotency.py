"""
Duplicate prevention, UNKNOWN blocking, and recovery across a restart.

Every test here describes a way the old design could have bought the same stock
twice, or held an unprotected position without knowing it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from broker.interface import BrokerUnreachable
from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    IntentStatus,
    OpportunityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    TradeAction,
    TradingMode,
    UserDecision,
)
from core.schemas import OrderRecord, OrderRequest, Position, TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore
from trading.order_intent import OrderIntent

pytestmark = pytest.mark.asyncio


def _candidate(symbol: str = "AAPL") -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal("100.00"),
        stop=Decimal("95.00"),
        target=Decimal("112.00"),
        risk_reward=2.4,
        reasons=["test setup"],
        strategy_version="test-v1",
    )


class _LostReplyBroker(MockPaperBroker):
    """Accepts the order, then loses the reply — the classic ambiguous submit.

    The order really exists at the broker. Our process simply never learns its
    id, which is exactly the situation that used to produce a second position.
    """

    def __init__(self) -> None:
        super().__init__()
        self.submit_count = 0
        self.lose_next_reply = True

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        if request.side == OrderSide.BUY:
            self.submit_count += 1
            record = OrderRecord(
                id=uuid4(),
                client_order_id=request.client_order_id,
                broker_order_id=str(uuid4()),
                symbol=request.symbol.upper(),
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                status=OrderStatus.FILLED,
                limit_price=request.limit_price,
                filled_avg_price=request.limit_price,
                filled_qty=request.qty,
                raw={"lost_reply": self.lose_next_reply},
            )
            self.orders.append(record)
            self.positions.append(
                Position(
                    id=uuid4(),
                    symbol=record.symbol,
                    qty=request.qty,
                    avg_entry=request.limit_price or Decimal(100),
                    stop_price=None,
                    target_price=None,
                    status=PositionStatus.OPEN,
                    opened_at=datetime.now(UTC),
                )
            )
            if self.lose_next_reply:
                self.lose_next_reply = False
                raise BrokerUnreachable("connection dropped after the broker accepted the order")
            return record
        return await super().place_order(request)


def _service(
    broker: MockPaperBroker,
    store: MemoryOpportunityStore,
    intents: MemoryOrderIntentStore,
    audit: InMemoryAudit | None = None,
) -> ExecutionService:
    return ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=audit or InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=intents,
    )


async def _approved_opportunity(broker: MockPaperBroker, store: MemoryOpportunityStore):
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    return store.create(_candidate(), risk, TradingMode.CONFIRMATION)


# ── The intent is durable before anything is transmitted ─────────────────────


async def test_the_intent_is_persisted_before_the_broker_is_contacted() -> None:
    set_kill_switch(False)
    broker = _LostReplyBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = await _approved_opportunity(broker, store)

    with pytest.raises(RuntimeError, match="ENTRY_STATE_UNKNOWN"):
        await _service(broker, store, intents).decide(opp.id, UserDecision.APPROVE)

    # The reply was lost, yet we still know what we sent and how to find it.
    recorded = intents.list_by_key_prefix(f"entry:{opp.id}:")
    assert len(recorded) == 1
    assert recorded[0].client_order_id
    assert recorded[0].status is IntentStatus.UNKNOWN


# ── Duplicate prevention ─────────────────────────────────────────────────────


async def test_a_retry_after_a_lost_reply_does_not_place_a_second_order() -> None:
    """The headline invariant: one logical entry, one broker order."""
    set_kill_switch(False)
    broker = _LostReplyBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    opp = await _approved_opportunity(broker, store)
    rid = uuid4()
    version = opp.decision_version

    with pytest.raises(RuntimeError, match="ENTRY_STATE_UNKNOWN"):
        await _service(broker, store, intents, audit).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=rid,
            expected_decision_version=version,
        )
    assert broker.submit_count == 1

    # Reconciliation returns the stuck card to the queue and the user retries
    # with the same request_id (transport retry), not a fresh click.
    store.release_stale_approving(older_than_sec=0)
    result = await _service(broker, store, intents, audit).decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=rid,
        expected_decision_version=version,
    )

    assert broker.submit_count == 1, "the retry must adopt the existing order, not send a new one"
    assert result.status is OpportunityStatus.EXECUTED
    assert any(e["event_type"] == "DuplicateOrderPrevented" for e in audit.events)
    assert len([p for p in broker.positions if p.symbol == "AAPL"]) == 1


async def test_recovery_reaches_the_same_place_after_a_process_restart() -> None:
    """Nothing survives in memory across the restart except the durable intent."""
    set_kill_switch(False)
    broker = _LostReplyBroker()
    intents = MemoryOrderIntentStore()  # stands in for the database
    store = MemoryOpportunityStore()
    opp = await _approved_opportunity(broker, store)
    rid = uuid4()
    version = opp.decision_version

    with pytest.raises(RuntimeError, match="ENTRY_STATE_UNKNOWN"):
        await _service(broker, store, intents).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=rid,
            expected_decision_version=version,
        )

    # ── process dies here; only the intent store and the broker survive ──
    store.release_stale_approving(older_than_sec=0)

    restarted = _service(broker, store, intents)
    result = await restarted.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=rid,
        expected_decision_version=version,
    )

    assert broker.submit_count == 1
    assert result.status is OpportunityStatus.EXECUTED
    # Protection covers exactly the quantity that was recovered, not the
    # quantity we originally intended to buy.
    entry = next(o for o in broker.orders if o.side is OrderSide.BUY)
    stops = [o for o in broker.orders if o.order_type is OrderType.STOP]
    assert len(stops) == 1
    assert stops[0].qty == entry.filled_qty
    assert not intents.unresolved_symbols()


async def test_the_same_idempotency_key_yields_one_intent() -> None:
    intents = MemoryOrderIntentStore()
    opportunity = uuid4()
    admission_id = uuid4()

    def _make() -> OrderIntent:
        return OrderIntent(
            idempotency_key=f"entry:{opportunity}:0",
            broker="MockPaperBroker",
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(100),
            opportunity_id=opportunity,
            approval_admission_record_id=admission_id,
            geometry_hash="test-geometry",
        )

    first, created_first = intents.create_or_get(_make())
    second, created_second = intents.create_or_get(_make())

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


# ── UNKNOWN blocks conflicting trading ───────────────────────────────────────


async def test_an_unknown_intent_blocks_a_new_entry_in_the_same_symbol() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()

    intents.create_or_get(
        OrderIntent(
            idempotency_key="entry:stuck:0",
            broker="MockPaperBroker",
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            status=IntentStatus.UNKNOWN,
            opportunity_id=uuid4(),
        )
    )

    opp = await _approved_opportunity(broker, store)
    with pytest.raises(RuntimeError, match="UNRESOLVED_BROKER_STATE"):
        await _service(broker, store, intents, audit).decide(opp.id, UserDecision.APPROVE)

    assert broker.positions == []
    assert any(e["event_type"] == "EntryBlockedByUnresolvedState" for e in audit.events)
    assert store.get(opp.id).status is OpportunityStatus.AWAITING_CONFIRMATION


async def test_an_unknown_intent_does_not_block_a_different_symbol() -> None:
    """Blocking is per symbol; one ambiguous order must not halt the whole desk."""
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()

    intents.create_or_get(
        OrderIntent(
            idempotency_key="entry:stuck:0",
            broker="MockPaperBroker",
            symbol="TSLA",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            status=IntentStatus.UNKNOWN,
        )
    )

    risk = RiskEngine().evaluate(
        _candidate("AAPL"), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate("AAPL"), risk, TradingMode.CONFIRMATION)
    result = await _service(broker, store, intents).decide(opp.id, UserDecision.APPROVE)

    assert result.status is OpportunityStatus.EXECUTED


async def test_a_resolved_intent_stops_blocking() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()

    stuck, _ = intents.create_or_get(
        OrderIntent(
            idempotency_key="entry:stuck:0",
            broker="MockPaperBroker",
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            status=IntentStatus.UNKNOWN,
        )
    )
    intents.transition(stuck.id, IntentStatus.CANCELED)

    opp = await _approved_opportunity(broker, store)
    result = await _service(broker, store, intents).decide(opp.id, UserDecision.APPROVE)

    assert result.status is OpportunityStatus.EXECUTED


async def test_the_risk_engine_rejects_a_symbol_with_unresolved_broker_state() -> None:
    """Blocking also applies upstream, so autopilot never proposes the trade."""
    from risk.risk_engine import RiskContext

    snapshot = await MockPaperBroker().get_portfolio()
    decision = RiskEngine().evaluate(
        _candidate("AAPL"),
        snapshot,
        context=RiskContext(unresolved_symbols=frozenset({"AAPL"})),
    )

    assert decision.verdict.value == "reject"
    assert "UNRESOLVED_BROKER_STATE" in decision.reasons
