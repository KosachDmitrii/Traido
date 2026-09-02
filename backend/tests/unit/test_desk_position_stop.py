"""Desk position stop display falls back to broker protective orders."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from core.enums import OrderSide, OrderStatus, OrderType
from core.schemas import OrderRecord
from trading.desk_positions import protective_stop_for_display


def _stop_order(
    *,
    symbol: str,
    qty: str,
    stop_price: str,
    broker_order_id: str,
) -> OrderRecord:
    return OrderRecord(
        id=uuid4(),
        client_order_id=f"traido-{broker_order_id[:8]}",
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        qty=Decimal(qty),
        status=OrderStatus.ACCEPTED,
        stop_price=Decimal(stop_price),
    )


def test_prefers_ledger_stop_when_present() -> None:
    orders = [_stop_order(symbol="LLY", qty="4", stop_price="1153.25", broker_order_id="abc")]
    assert protective_stop_for_display(
        symbol="LLY",
        qty=Decimal(4),
        open_orders=orders,
        ledger_stop=Decimal("1160.00"),
        stop_order_id="abc",
    ) == Decimal("1160.00")


def test_falls_back_to_linked_stop_order_id() -> None:
    orders = [_stop_order(symbol="LLY", qty="4", stop_price="1153.25", broker_order_id="abc")]
    assert protective_stop_for_display(
        symbol="LLY",
        qty=Decimal(4),
        open_orders=orders,
        ledger_stop=None,
        stop_order_id="abc",
    ) == Decimal("1153.25")


def test_falls_back_to_symbol_stop_when_id_missing() -> None:
    orders = [_stop_order(symbol="LLY", qty="4", stop_price="1153.25", broker_order_id="abc")]
    assert protective_stop_for_display(
        symbol="LLY",
        qty=Decimal(4),
        open_orders=orders,
        ledger_stop=None,
        stop_order_id=None,
    ) == Decimal("1153.25")


def test_returns_none_when_no_protective_stop() -> None:
    assert (
        protective_stop_for_display(
            symbol="LLY",
            qty=Decimal(4),
            open_orders=[],
            ledger_stop=None,
            stop_order_id=None,
        )
        is None
    )
