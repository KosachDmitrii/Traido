"""
Correlating a durable Traido intent with an IBKR order after a reconnect.

IB gives an order three identifiers and only one of them is durable:

- `orderId`   scoped to a clientId session, and reused after a restart
- `permId`    account-wide and permanent
- `orderRef`  a free-text field we own, carrying Traido's client_order_id

So recovery looks up by `orderRef` (the handle we chose before transmitting)
and stores `permId` (the handle IB guarantees). `orderId` alone is never enough,
which is what these tests pin down.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from broker.ibkr import FakeIBKRTransport, IBKRBroker
from core.enums import IntentPurpose, IntentStatus, OrderSide, OrderType
from core.schemas import OrderRequest
from trading.order_intent import OrderIntent, exit_idempotency_key, locate_broker_order

pytestmark = pytest.mark.asyncio


def _intent(client_order_id: str, *, broker_order_id: str | None = None) -> OrderIntent:
    return OrderIntent(
        idempotency_key=exit_idempotency_key(uuid4(), 0),
        purpose=IntentPurpose.EXIT,
        broker="IBKRBroker",
        symbol="AAPL",
        side=OrderSide.SELL,
        requested_qty=Decimal(10),
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        status=IntentStatus.SUBMITTING if broker_order_id is None else IntentStatus.SUBMITTED,
    )


def _sell(client_order_id: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        qty=Decimal(10),
        reason="correlation test",
    )


async def test_the_client_order_id_travels_to_ib_as_the_order_ref() -> None:
    transport = FakeIBKRTransport(fill_ratio=0.0)
    broker = IBKRBroker(transport)

    await broker.place_order(_sell("traido-x-abc123"))

    stored = next(iter(transport.orders.values()))
    assert stored["orderRef"] == "traido-x-abc123"


async def test_an_order_is_recoverable_by_order_ref_alone() -> None:
    """The restart case: the process died before it learned any IB id."""
    transport = FakeIBKRTransport(fill_ratio=1.0)
    await IBKRBroker(transport).place_order(_sell("traido-x-lost-reply"))

    # A new process, a new adapter, a new clientId — same durable intent.
    recovered = await locate_broker_order(IBKRBroker(transport), _intent("traido-x-lost-reply"))

    assert recovered is not None
    assert recovered.client_order_id == "traido-x-lost-reply"
    assert recovered.filled_qty == Decimal(10)


async def test_the_permanent_id_is_surfaced_for_persistence() -> None:
    transport = FakeIBKRTransport(fill_ratio=1.0)
    broker = IBKRBroker(transport)

    record = await broker.place_order(_sell("traido-x-perm"))

    assert record.raw.get("permId"), "permId is what survives a reconnect"
    recovered = await broker.find_order_by_client_id("traido-x-perm")
    assert recovered is not None
    assert recovered.raw["permId"] == record.raw["permId"]


async def test_recovery_does_not_depend_on_an_order_id_held_in_memory() -> None:
    """Two orders, one recovered by ref: the ref must select the right one."""
    transport = FakeIBKRTransport(fill_ratio=0.0)
    broker = IBKRBroker(transport)
    await broker.place_order(_sell("traido-x-one"))
    second = await broker.place_order(_sell("traido-x-two"))

    found = await locate_broker_order(broker, _intent("traido-x-two"))

    assert found is not None
    assert found.broker_order_id == second.broker_order_id


async def test_a_forgotten_order_is_unresolved_rather_than_absent() -> None:
    transport = FakeIBKRTransport(fill_ratio=1.0, forget_orders=True)
    broker = IBKRBroker(transport)
    submitted = await broker.place_order(_sell("traido-x-ghost"))

    intent = _intent("traido-x-ghost", broker_order_id=submitted.broker_order_id)

    assert await locate_broker_order(broker, intent) is None
