"""Acknowledged order polling retries reads, never submissions."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from broker.interface import BrokerUnreachable
from broker.paper.mock import MockPaperBroker
from core.enums import OrderSide, OrderType
from core.schemas import OrderRequest
from trading.fills import wait_for_fill


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["recover", "outage", "slow", "cancel"])
async def test_acknowledged_fill_read_failure_does_not_resubmit(mode):
    broker = MockPaperBroker()
    filled = await broker.place_order(
        OrderRequest(
            client_order_id=str(uuid4()),
            reason="synthetic polling regression",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(1),
            limit_price=Decimal(100),
        )
    )
    if mode == "recover":
        broker.get_order = AsyncMock(side_effect=[BrokerUnreachable("offline"), filled])
    elif mode == "slow":

        async def stalled_read(_order_id):
            await asyncio.Event().wait()

        broker.get_order = AsyncMock(side_effect=stalled_read)
    else:
        broker.get_order = AsyncMock(side_effect=BrokerUnreachable("offline"))
    if mode == "recover":
        result = await wait_for_fill(
            broker, filled.broker_order_id, timeout_sec=0.1, poll_sec=0.001
        )
        assert result.filled_qty == Decimal(1)
        assert broker.get_order.await_count == 2
    elif mode in {"outage", "slow"}:
        with pytest.raises(RuntimeError, match="FILL_TIMEOUT"):
            await wait_for_fill(broker, filled.broker_order_id, timeout_sec=0.01, poll_sec=0.001)
    else:
        task = asyncio.create_task(wait_for_fill(broker, filled.broker_order_id, poll_sec=0.001))
        await asyncio.sleep(0.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(broker.orders) == 1
