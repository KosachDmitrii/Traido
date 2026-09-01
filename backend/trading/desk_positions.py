"""Display-only helpers for desk position cards.

Stop/target on a card are the strategy plan from the ledger when we have it.
When the book lost stop_price after a messy entry/exit, the resting protective
stop at the broker is still the number the operator needs to compare against —
read it for display only, never to gate a decision.
"""

from __future__ import annotations

from decimal import Decimal

from core.enums import OrderSide, OrderType
from core.schemas import OrderRecord

_STOP_ORDER_TYPES = frozenset({OrderType.STOP, OrderType.STOP_LIMIT})


def protective_stop_for_display(
    *,
    symbol: str,
    qty: Decimal,
    open_orders: list[OrderRecord],
    ledger_stop: Decimal | None,
    stop_order_id: str | None = None,
) -> Decimal | None:
    """Ledger stop when recorded; else the broker's resting protective stop."""
    if ledger_stop is not None:
        return ledger_stop

    sym = symbol.upper()

    if stop_order_id:
        for order in open_orders:
            if order.broker_order_id == stop_order_id and order.stop_price is not None:
                return order.stop_price

    candidates = [
        order
        for order in open_orders
        if order.symbol.upper() == sym
        and order.side == OrderSide.SELL
        and order.order_type in _STOP_ORDER_TYPES
        and order.stop_price is not None
    ]
    if not candidates:
        return None

    for order in candidates:
        if order.qty == qty:
            return order.stop_price

    if len(candidates) == 1:
        return candidates[0].stop_price

    return min(candidates, key=lambda order: abs(order.qty - qty)).stop_price
