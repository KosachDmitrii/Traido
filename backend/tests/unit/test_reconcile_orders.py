"""Reconcile orphan entry orders left after fill timeout."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.enums import OrderSide, OrderStatus, OrderType
from core.schemas import OrderRecord
from trading.reconcile import cancel_orphaned_entry_orders


class _Canceller:
    """Stands in for the execution service's cancel surface."""

    def __init__(self, broker) -> None:
        self.broker = broker

    async def cancel_entry_order(self, *, broker_order_id: str, symbol: str, reason: str) -> bool:
        await self.broker.cancel_order(broker_order_id)
        return True


@pytest.mark.asyncio
async def test_cancel_orphaned_traido_entry_orders(monkeypatch) -> None:
    monkeypatch.setattr("trading.reconcile._any_approving", lambda: False)
    broker = MockPaperBroker()
    broker.orders.append(
        OrderRecord(
            id=uuid4(),
            client_order_id="traido-e-abcdef1234-deadbeef",
            broker_order_id="oid-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(10),
            status=OrderStatus.ACCEPTED,
            limit_price=Decimal(100),
        )
    )
    # Non-Traido order must stay
    broker.orders.append(
        OrderRecord(
            id=uuid4(),
            client_order_id="manual-1",
            broker_order_id="oid-2",
            symbol="MSFT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(1),
            status=OrderStatus.ACCEPTED,
            limit_price=Decimal(400),
        )
    )
    n = await cancel_orphaned_entry_orders(broker, execution=_Canceller(broker))
    assert n == 1
    by_id = {o.broker_order_id: o for o in broker.orders}
    assert by_id["oid-1"].status == OrderStatus.CANCELED
    assert by_id["oid-2"].status == OrderStatus.ACCEPTED


async def test_the_sweep_leaves_orders_alone_without_an_execution_service() -> None:
    """Cancelling is a broker mutation and goes through the service like placing does.

    Reaching past the execution layer when the dependency is missing is how the
    exception the runtime-path audit found became permanent: reconciliation
    cancelled entry orders directly, and the static guard only covered
    `place_order`, so nothing objected for as long as it existed.
    """
    broker = MockPaperBroker()
    broker.orders.append(
        OrderRecord(
            id=uuid4(),
            client_order_id="traido-e-abcdef1234-deadbeef",
            broker_order_id="oid-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(10),
            status=OrderStatus.ACCEPTED,
            limit_price=Decimal(100),
        )
    )

    assert await cancel_orphaned_entry_orders(broker) == 0
    assert broker.orders[0].status == OrderStatus.ACCEPTED


@pytest.mark.asyncio
async def test_skip_orphan_cancel_while_approving(monkeypatch) -> None:
    monkeypatch.setattr("trading.reconcile._any_approving", lambda: True)
    broker = MockPaperBroker()
    broker.orders.append(
        OrderRecord(
            id=uuid4(),
            client_order_id="traido-e-abcdef1234-alive",
            broker_order_id="oid-3",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(10),
            status=OrderStatus.ACCEPTED,
            limit_price=Decimal(100),
        )
    )
    n = await cancel_orphaned_entry_orders(broker)
    assert n == 0
    assert broker.orders[0].status == OrderStatus.ACCEPTED
