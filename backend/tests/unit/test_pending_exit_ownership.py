"""A broker-flat pending sale is reconciled, never submitted a second time."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.audit import InMemoryAudit
from core.config import get_settings
from core.enums import IntentStatus, OrderStatus, TradeAction, UserDecision
from core.schemas import ExitProposal
from strategy.orb.loop import exit_due_positions
from tests.unit.test_exit_reconciliation import _Broker, _exit_intent, _position, _sell
from trading.execution import ExecutionService
from trading.exits import EXIT_APPROVING, EXIT_SOLD, MemoryExitStore
from trading.intents import MemoryOrderIntentStore
from trading.ledger import LEDGER
from trading.opportunities import MemoryOpportunityStore
from trading.reconcile import reconcile_order_intents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [IntentStatus.SUBMITTED, IntentStatus.PARTIALLY_FILLED, IntentStatus.UNKNOWN, None]
)
async def test_pending_sale_is_retained_when_broker_is_flat(monkeypatch, status):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    pid = _position(LEDGER, qty=Decimal(100))
    exits = MemoryExitStore()
    item = exits.upsert(
        ExitProposal(
            position_id=pid,
            symbol="AAPL",
            action=TradeAction.SELL,
            entry=Decimal(100),
            current=Decimal(110),
            pnl_pct=10,
            reasons=["ORB_TARGET_REACHED"],
            recommendation=UserDecision.SELL,
            confidence=1,
        )
    )
    item = exits.claim(item.id, from_status=item.status, to_status=EXIT_APPROVING)
    intents = MemoryOrderIntentStore()
    if status is not None:
        _exit_intent(intents, pid, status=status, broker_order_id="x-1")
    remaining = Decimal(100)
    if status is IntentStatus.PARTIALLY_FILLED:
        partial = _sell(oid="x-1", status=OrderStatus.PARTIAL, qty=Decimal(100), filled=Decimal(30))
        await reconcile_order_intents(_Broker(orders=[partial]), intents, exit_store=exits)
        remaining = Decimal(70)
        assert LEDGER.get(pid).qty == remaining
    broker = SimpleNamespace(list_positions=AsyncMock(return_value=[]), place_order=AsyncMock())
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=MemoryOpportunityStore(),
        exit_store=exits,
        intents=intents,
    )
    for _ in range(2):
        if status is IntentStatus.UNKNOWN:
            with pytest.raises(RuntimeError, match="EXIT_STATE_UNKNOWN"):
                await service.close_position("AAPL", reason="ORB_TARGET_REACHED")
        else:
            assert await service.close_position("AAPL", reason="ORB_TARGET_REACHED") == item
    broker.list_positions.assert_not_awaited()
    broker.place_order.assert_not_awaited()
    assert exits.get(item.id).status == EXIT_APPROVING
    assert LEDGER.get(pid).qty == remaining
    if status is not None:
        filled = _sell(oid="x-1", status=OrderStatus.FILLED, qty=Decimal(100), filled=Decimal(100))
        await reconcile_order_intents(_Broker(orders=[filled]), intents, exit_store=exits)
        assert LEDGER.get(pid).status == "closed"
        assert exits.get(item.id).status == EXIT_SOLD
        # Repeated recovery cannot apply the sale to the ledger twice.
        await reconcile_order_intents(_Broker(orders=[filled]), intents, exit_store=exits)
        assert LEDGER.get(pid).qty == 0


@pytest.mark.asyncio
async def test_target_loop_does_not_request_an_exit_already_owned(monkeypatch):
    from api import deps
    from trading.exits import EXITS

    pid = uuid4()
    pending = SimpleNamespace(proposal=SimpleNamespace(position_id=pid))
    row = SimpleNamespace(
        id=pid,
        symbol="TECK",
        payload={"exit_policy": "session_close", "exit_at": "2026-10-09T20:00:00+00:00"},
    )
    monkeypatch.setattr(EXITS, "list_pending", lambda: [pending])
    monkeypatch.setattr(LEDGER, "get_open", lambda: [row])
    build = AsyncMock()
    monkeypatch.setattr(deps, "build_execution_service", build)
    assert await exit_due_positions() == 0
    build.assert_not_called()


@pytest.mark.asyncio
async def test_claim_for_another_position_never_authorizes_or_completes_this_exit():
    pid = _position(LEDGER, qty=Decimal(100))
    exits = MemoryExitStore()
    item = exits.upsert(
        ExitProposal(
            position_id=uuid4(),
            symbol="AAPL",
            action=TradeAction.SELL,
            entry=Decimal(100),
            current=Decimal(110),
            pnl_pct=10,
            reasons=["ORB_TARGET_REACHED"],
            recommendation=UserDecision.SELL,
            confidence=1,
        )
    )
    exits.claim(item.id, from_status=item.status, to_status=EXIT_APPROVING)
    broker = SimpleNamespace(list_positions=AsyncMock(return_value=[]), place_order=AsyncMock())
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=MemoryOpportunityStore(),
        exit_store=exits,
        intents=MemoryOrderIntentStore(),
    )
    with pytest.raises(ValueError, match="no_open_position"):
        await service.close_position("AAPL")
    assert LEDGER.get(pid).qty == 100
    assert exits.get(item.id).status == EXIT_APPROVING
    broker.place_order.assert_not_awaited()
