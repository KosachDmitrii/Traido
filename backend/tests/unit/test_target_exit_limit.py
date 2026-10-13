"""Automatic exits preserve the stored target at the broker boundary."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from broker.interface import BrokerRejection
from core.audit import InMemoryAudit
from core.enums import OrderType
from core.schemas import Quote
from trading.execution import ExecutionService
from trading.intents import MemoryOrderIntentStore
from trading.ledger import LEDGER
from trading.opportunities import MemoryOpportunityStore


def setup(monkeypatch, *, now=None, bid="110", target="110", entry="100", feed="sip", age=0):
    now = now or datetime(2026, 10, 13, 15, tzinfo=UTC)
    row = SimpleNamespace(id=uuid4(), symbol="AAPL", avg_entry=Decimal(entry), payload={})
    monkeypatch.setattr(LEDGER, "get", lambda pid: row if pid == row.id else None)
    monkeypatch.setattr(
        "strategy.orb.position_policy.observed_target", lambda payload: Decimal(target)
    )
    quote = Quote(
        symbol="AAPL",
        bid=Decimal(bid),
        ask=Decimal(bid) + Decimal("0.01"),
        ts=now - timedelta(seconds=age),
        feed=feed,
        source="alpaca",
    )
    broker = SimpleNamespace(place_order=AsyncMock())
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=MemoryOpportunityStore(),
        intents=MemoryOrderIntentStore(),
        quotes=SimpleNamespace(get_quote=AsyncMock(return_value=quote)),
        clock=lambda: now,
    )
    item = SimpleNamespace(
        id=uuid4(),
        proposal=SimpleNamespace(position_id=row.id, symbol="AAPL", reasons=["ORB_TARGET_REACHED"]),
    )
    return service, item, broker


@pytest.mark.asyncio
async def test_target_intent_is_durable_limit(monkeypatch):
    service, item, _ = setup(monkeypatch, target="110.001", bid="110.02")
    intent = await service._create_exit_intent(item, qty=Decimal("10.25"))
    assert intent.order_type is OrderType.LIMIT
    assert intent.limit_price == Decimal("110.01")
    recovered = type(intent).model_validate_json(intent.model_dump_json())
    assert recovered.limit_price == intent.limit_price
    assert recovered.risk_snapshot == {"target_session": "2026-10-13"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"bid": "109"},
        {"age": 6},
        {"age": -2},
        {"feed": "iex"},
        {"entry": "110"},
        {"target": "NaN"},
        {"now": datetime(2026, 10, 13, 20, 17, tzinfo=UTC)},
        {"now": datetime(2026, 10, 13, 19, 59, 30, tzinfo=UTC)},
        {"now": datetime(2026, 10, 10, 15, tzinfo=UTC)},
        {"now": datetime(2026, 11, 27, 18, tzinfo=UTC)},
    ],
)
async def test_invalid_target_exit_never_creates_intent(monkeypatch, changes):
    service, item, broker = setup(monkeypatch, **changes)
    with pytest.raises(BrokerRejection):
        await service._create_exit_intent(item, qty=Decimal(10))
    assert service.intents.list_unresolved() == []
    broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_exit_remains_market(monkeypatch):
    service, item, _ = setup(monkeypatch, bid="90")
    item.proposal.reasons = ["operator_close"]
    intent = await service._create_exit_intent(item, qty=Decimal(10))
    assert intent.order_type is OrderType.MARKET
    assert intent.limit_price is None


@pytest.mark.asyncio
async def test_submit_preserves_limit_at_broker_boundary(monkeypatch):
    from core.enums import OrderStatus
    from tests.unit.test_exit_reconciliation import _sell

    service, item, broker = setup(monkeypatch)
    intent = await service._create_exit_intent(item, qty=Decimal(10))
    service._free_shares_for_exit = AsyncMock()
    service._verify_long_exit_quantity = AsyncMock()
    broker.place_order.return_value = _sell(
        oid="limit-1", status=OrderStatus.ACCEPTED, qty=Decimal(10), filled=Decimal(0)
    )
    await service._place_exit(intent, item, stop_order_id=None)
    request = broker.place_order.call_args.args[0]
    assert request.order_type is OrderType.LIMIT
    assert request.limit_price == Decimal(110)
    assert request.qty == Decimal(10)


@pytest.mark.asyncio
async def test_price_drop_between_intent_and_submit_retires_without_sale(monkeypatch):
    from core.enums import IntentStatus

    service, item, broker = setup(monkeypatch)
    intent = await service._create_exit_intent(item, qty=Decimal(10))
    service._free_shares_for_exit = AsyncMock()
    service._verify_long_exit_quantity = AsyncMock()
    quote = service.quotes.get_quote.return_value
    service.quotes.get_quote.return_value = quote.model_copy(update={"bid": Decimal(90)})
    with pytest.raises(BrokerRejection):
        await service._place_exit(intent, item, stop_order_id=None)
    broker.place_order.assert_not_awaited()
    assert service.intents.get(intent.id).status is IntentStatus.REJECTED


@pytest.mark.asyncio
async def test_created_intent_cannot_cross_session(monkeypatch):
    service, item, broker = setup(monkeypatch)
    intent = await service._create_exit_intent(item, qty=Decimal(10))
    service._free_shares_for_exit = AsyncMock()
    service._verify_long_exit_quantity = AsyncMock()
    service._clock = lambda: datetime(2026, 10, 14, 15, tzinfo=UTC)
    with pytest.raises(BrokerRejection, match="SESSION_EXPIRED"):
        await service._place_exit(intent, item, stop_order_id=None)
    broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_end_cancels_and_absorbs_partial_fill_once(monkeypatch):
    from core.enums import IntentStatus, OrderStatus
    from tests.unit.test_exit_reconciliation import _Broker, _exit_intent, _position, _sell
    from trading.reconcile import reconcile_order_intents

    pid = _position(LEDGER, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    intent = _exit_intent(intents, pid, status=IntentStatus.SUBMITTED, broker_order_id="x-1")
    intents.update_fields(
        intent.id,
        exit_reason="ORB_TARGET_REACHED",
        risk_snapshot={"target_session": "2026-10-13"},
        order_type=OrderType.LIMIT,
        limit_price=Decimal(110),
    )
    terminal = _sell(oid="x-1", status=OrderStatus.CANCELED, qty=Decimal(100), filled=Decimal(30))
    broker = _Broker(orders=[terminal])
    broker.get_order = AsyncMock(
        side_effect=[
            _sell(oid="x-1", status=OrderStatus.PARTIAL, qty=Decimal(100), filled=Decimal(30)),
            terminal,
        ]
    )
    broker.cancel_order = AsyncMock(return_value=terminal)
    monkeypatch.setattr("trading.session_hours.us_equity_rth_open", lambda: False)
    await reconcile_order_intents(broker, intents)
    broker.cancel_order.assert_awaited_once_with("x-1")
    assert LEDGER.get(pid).qty == 70
    broker.get_order = AsyncMock(return_value=terminal)
    await reconcile_order_intents(broker, intents)
    assert LEDGER.get(pid).qty == 70
