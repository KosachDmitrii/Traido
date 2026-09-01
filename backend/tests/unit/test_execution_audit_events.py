"""
The audit vocabulary is an interface.

Operators, alerting, and post-mortems all key off these names. Renaming one is
a breaking change, so the vocabulary is pinned here, and the important ones are
proven to actually fire rather than merely to exist as string literals.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from core.audit import InMemoryAudit
from core.enums import IntentStatus, OrderSide, OrderStatus, OrderType
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.intents import MemoryOrderIntentStore
from trading.order_intent import OrderIntent

REPO = Path(__file__).resolve().parents[2]

REQUIRED_EVENTS = [
    "OrderIntentCreated",
    "OrderSubmitStarted",
    "OrderSubmitted",
    "OrderAcknowledged",
    "OrderPartiallyFilled",
    "OrderFilled",
    "OrderCancelRequested",
    "OrderCancelled",
    "OrderRejected",
    "OrderExpired",
    "EntryStateUnknown",
    "ReconciliationStarted",
    "ReconciliationResolved",
    "ReconciliationUnresolved",
    "LiquidityGateRejected",
    "RTHGateRejected",
    "DuplicateOrderPrevented",
    "ProtectiveOrderMissing",
    "ProtectiveOrderRecovered",
    "EmergencyCloseTriggered",
    # Stage 7.1 — the exit side.
    "ExitIntentCreated",
    "ExitSubmitStarted",
    "ExitSubmitted",
    "ExitAcknowledged",
    "ExitPartiallyFilled",
    "ExitFilled",
    "ExitCancelRequested",
    "ExitCancelled",
    "ExitRejected",
    "ExitStateUnknown",
    "ExitBlockedByUnresolvedState",
    "ExitBlockedByBrokerState",
    "ExitFillReconciled",
    "EmergencyExitSubmitted",
    "EmergencyExitUnknown",
    "ProtectionResizeRequested",
    "ProtectionResized",
    "ProtectionResizeFailed",
    "ProtectionQuantityMismatch",
    "PositionQuantityReconciled",
    "PositionQuantityMismatch",
    "EntryBlockedByOpenPosition",
    "ProtectionUnverified",
]


@pytest.mark.parametrize("event", REQUIRED_EVENTS)
def test_the_execution_audit_vocabulary_exists(event: str) -> None:
    sources = "\n".join(
        path.read_text() for path in (REPO / "trading").rglob("*.py") if path.is_file()
    )
    assert f'"{event}"' in sources, f"{event} is no longer emitted anywhere in trading/"


@pytest.mark.asyncio
async def test_a_broker_expiry_is_audited_as_an_expiry() -> None:
    """The generic 'reconciled' event is not specific enough to alert on."""
    from uuid import uuid4

    from broker.paper.mock import MockPaperBroker
    from core.schemas import OrderRecord
    from trading.reconcile import reconcile_order_intents

    broker = MockPaperBroker()
    broker.orders.append(
        OrderRecord(
            id=uuid4(),
            client_order_id="traido-e-expiry",
            broker_order_id="expired-order",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(10),
            status=OrderStatus.EXPIRED,
            limit_price=Decimal(100),
        )
    )
    intents = MemoryOrderIntentStore()
    intents.create_or_get(
        OrderIntent(
            idempotency_key="entry:expiry:0",
            broker="MockPaperBroker",
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            status=IntentStatus.ACKNOWLEDGED,
            broker_order_id="expired-order",
        )
    )
    audit = InMemoryAudit()

    await reconcile_order_intents(broker, intents, audit)

    assert any(e["event_type"] == "OrderExpired" for e in audit.events)


@pytest.mark.asyncio
async def test_audit_payloads_from_a_real_entry_carry_no_credentials() -> None:
    """Checks what is actually emitted, not what the source happens to mention."""
    import json

    from broker.paper.mock import MockPaperBroker
    from core.enums import TradeAction, TradingMode, UserDecision
    from core.schemas import TradeCandidate
    from risk.kill_switch import set_kill_switch
    from risk.risk_engine import RiskEngine
    from trading.execution import ExecutionService
    from trading.exits import MemoryExitStore
    from trading.opportunities import MemoryOpportunityStore

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    candidate = TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal("100.00"),
        stop=Decimal("95.00"),
        target=Decimal("112.00"),
        risk_reward=2.4,
        reasons=["audit test"],
        strategy_version="test-v1",
    )
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    audit = InMemoryAudit()

    await ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=audit,
        store=store,
        exit_store=MemoryExitStore(),
        intents=MemoryOrderIntentStore(),
    ).decide(opp.id, UserDecision.APPROVE)

    assert audit.events, "the entry path must leave an audit trail"
    forbidden = ("api_key", "api_secret", "apca-api", "password", "authorization")
    for event in audit.events:
        serialized = json.dumps(event["payload"], default=str).lower()
        for needle in forbidden:
            assert needle not in serialized, f"{event['event_type']} leaked {needle}"
