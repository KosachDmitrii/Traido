"""Fill polling helpers — ExecutionService only."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from core.enums import OrderStatus
from core.ports import BrokerPort
from core.schemas import OrderRecord

TERMINAL_OK = {OrderStatus.FILLED}
TERMINAL_BAD = {OrderStatus.CANCELED, OrderStatus.REJECTED}


async def wait_for_fill(
    broker: BrokerPort,
    broker_order_id: str,
    *,
    timeout_sec: float = 45.0,
    poll_sec: float = 1.0,
) -> OrderRecord:
    """Poll until FILLED or raise on cancel/reject/timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_sec
    last: OrderRecord | None = None
    while asyncio.get_event_loop().time() < deadline:
        last = await broker.get_order(broker_order_id)
        if last.status in TERMINAL_OK:
            if last.filled_avg_price is None or last.filled_avg_price <= 0:
                # Some brokers omit avg on instant fill — fall back to limit/stop
                px = last.limit_price or last.stop_price
                if px and px > 0:
                    last = last.model_copy(
                        update={
                            "filled_avg_price": px,
                            "filled_qty": last.filled_qty or last.qty,
                        }
                    )
                else:
                    raise RuntimeError(f"FILL_MISSING_PRICE:{broker_order_id}")
            return last
        if last.status in TERMINAL_BAD:
            raise RuntimeError(f"ORDER_{last.status.value.upper()}:{broker_order_id}")
        await asyncio.sleep(poll_sec)
    raise RuntimeError(f"FILL_TIMEOUT:{broker_order_id}")


def fill_price(order: OrderRecord) -> Decimal:
    if order.filled_avg_price and order.filled_avg_price > 0:
        return order.filled_avg_price
    if order.limit_price and order.limit_price > 0:
        return order.limit_price
    raise RuntimeError("no_fill_price")
