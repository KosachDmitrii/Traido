"""
IBKR broker adapter.

Its only job is translation: IB vocabulary in, Traido domain objects out. No IB
status string may escape this file, because the moment `PreSubmitted` reaches
the risk engine, the broker has leaked into the domain and swapping brokers
stops being a configuration change.

Status: implemented and contract-tested against `FakeIBKRTransport`. It has
never spoken to an IB Gateway — see `docs/architecture/vendor-lock.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from broker.ibkr.instruments import (
    IBKRInstrumentResolver,
    Instrument,
    InstrumentError,
    InstrumentResolver,
)
from broker.ibkr.transport import IBKRTransport, UnconfiguredIBKRTransport
from broker.interface import BrokerRejection, BrokerUnreachable, assert_paper_only
from core.enums import (
    BrokerConnectionState,
    BrokerEnvironment,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
)
from core.schemas import OrderRecord, OrderRequest, PortfolioSnapshot, Position
from risk.kill_switch import is_kill_switch_on

IB_STATUS_MAP: dict[str, OrderStatus] = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "ApiPending": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.ACCEPTED,
    "Submitted": OrderStatus.ACCEPTED,
    "PendingCancel": OrderStatus.ACCEPTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELED,
    "ApiCancelled": OrderStatus.CANCELED,
    "Inactive": OrderStatus.REJECTED,
}

_ORDER_TYPE_TO_IB = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "LMT",
    OrderType.STOP: "STP",
    OrderType.STOP_LIMIT: "STP LMT",
}

_IB_TO_ORDER_TYPE = {v: k for k, v in _ORDER_TYPE_TO_IB.items()}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


class IBKRBroker:
    """Traido's `BrokerPort` over an IB transport."""

    def __init__(
        self,
        transport: IBKRTransport | None = None,
        *,
        environment: str = BrokerEnvironment.PAPER.value,
        account_id: str | None = None,
        resolver: InstrumentResolver | None = None,
    ) -> None:
        self._transport = transport or UnconfiguredIBKRTransport()
        self._environment = environment
        self.account_id = account_id
        self._resolver = resolver or IBKRInstrumentResolver(self._transport)
        assert_paper_only(self.environment)

    @property
    def environment(self) -> str:
        return self._environment

    def connection_state(self) -> BrokerConnectionState:
        """Surface the session's health so execution can refuse new exposure."""
        reporter = getattr(self._transport, "connection_state", None)
        if reporter is None:
            return BrokerConnectionState.READY
        state = reporter()
        return state if isinstance(state, BrokerConnectionState) else BrokerConnectionState.DEGRADED

    async def resolve_instrument(self, symbol: str) -> Instrument:
        """One symbol, one contract, or an error. Never a guess."""
        return await self._resolver.resolve(symbol)

    async def get_portfolio(self) -> PortfolioSnapshot:
        summary = await self._call(self._transport.account_summary())
        positions = await self.list_positions()
        equity = _dec(summary.get("NetLiquidation")) or Decimal(0)
        cash = _dec(summary.get("TotalCashValue")) or Decimal(0)
        exposure = sum((p.qty * p.avg_entry for p in positions), Decimal(0))
        return PortfolioSnapshot(
            equity=equity,
            cash=cash,
            buying_power=_dec(summary.get("BuyingPower")) or cash,
            open_exposure=exposure,
            open_positions=len(positions),
            day_pnl=_dec(summary.get("RealizedPnL")) or Decimal(0),
            week_pnl=Decimal(0),
            drawdown_pct=0.0,
            kill_switch=is_kill_switch_on(),
        )

    async def list_positions(self) -> list[Position]:
        rows = await self._call(self._transport.positions())
        return [
            Position(
                id=uuid4(),
                symbol=str(row["symbol"]).upper(),
                qty=_dec(row.get("position")) or Decimal(0),
                avg_entry=_dec(row.get("avgCost")) or Decimal(0),
                stop_price=None,
                target_price=None,
                status=PositionStatus.OPEN,
                opened_at=datetime.now(UTC),
            )
            for row in rows
            if (_dec(row.get("position")) or Decimal(0)) != 0
        ]

    async def list_open_orders(self) -> list[OrderRecord]:
        rows = await self._call(self._transport.open_orders())
        return [self._map_order(r) for r in rows]

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        if is_kill_switch_on() and not request.reduces_risk:
            raise RuntimeError("KILL_SWITCH blocks new exposure")

        # Identity before transmission. An ambiguous ticker is a rejection, not
        # something to let IB's router decide on our behalf.
        try:
            instrument = await self._resolver.resolve(request.symbol)
        except InstrumentError as exc:
            raise BrokerRejection(f"ibkr contract resolution failed: {exc}") from exc

        payload: dict[str, Any] = {
            "conId": instrument.con_id,
            "exchange": instrument.exchange,
            "currency": instrument.currency,
            "symbol": request.symbol.upper(),
            "action": "BUY" if request.side == OrderSide.BUY else "SELL",
            "orderType": _ORDER_TYPE_TO_IB[request.order_type],
            "totalQuantity": str(request.qty),
            "tif": "GTC" if request.order_type == OrderType.STOP else "DAY",
            # IB's own idempotency handle; Traido's client_order_id rides here.
            "orderRef": request.client_order_id,
        }
        if request.limit_price is not None:
            payload["lmtPrice"] = str(request.limit_price)
        if request.stop_price is not None:
            payload["auxPrice"] = str(request.stop_price)

        raw = await self._call(self._transport.place_order(payload), submitting=True)
        return self._map_order(raw)

    async def cancel_order(self, broker_order_id: str) -> OrderRecord:
        raw = await self._call(self._transport.cancel_order(broker_order_id))
        return self._map_order(raw)

    async def get_order(self, broker_order_id: str) -> OrderRecord:
        raw = await self._call(self._transport.get_order(broker_order_id))
        return self._map_order(raw)

    async def find_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        if not client_order_id:
            return None
        rows = await self._call(self._transport.orders_by_ref(client_order_id))
        return self._map_order(rows[0]) if rows else None

    async def _call(self, awaitable: Any, *, submitting: bool = False) -> Any:
        """Translate transport failures into the two answers execution can act on.

        A refusal is safe to retry against; anything else is ambiguous and must
        stay ambiguous, so the order-intent machinery can mark it UNKNOWN.
        """
        try:
            return await awaitable
        except (ValueError, KeyError) as exc:
            # IB reports business-level refusals as error codes, not transport
            # failures: the gateway answered, and the answer was no.
            if submitting:
                raise BrokerRejection(f"ibkr rejected order: {exc}") from exc
            raise BrokerUnreachable(f"ibkr request failed: {exc}") from exc
        except Exception as exc:
            raise BrokerUnreachable(f"ibkr transport failure: {exc}") from exc

    def _map_order(self, raw: dict[str, Any]) -> OrderRecord:
        filled = _dec(raw.get("filled")) or Decimal(0)
        total = _dec(raw.get("totalQuantity")) or Decimal(0)
        native = str(raw.get("status") or "")
        status = IB_STATUS_MAP.get(native, OrderStatus.SUBMITTED)

        # IB signals a partial through quantities, not through a status of its
        # own, so the normalization has to look at both.
        if status is OrderStatus.ACCEPTED and 0 < filled < total:
            status = OrderStatus.PARTIAL

        return OrderRecord(
            id=uuid4(),
            client_order_id=str(raw.get("orderRef") or ""),
            broker_order_id=str(raw.get("orderId") or ""),
            symbol=str(raw.get("symbol") or "").upper(),
            side=OrderSide.BUY if str(raw.get("action")) == "BUY" else OrderSide.SELL,
            order_type=_IB_TO_ORDER_TYPE.get(str(raw.get("orderType")), OrderType.MARKET),
            qty=total,
            status=status,
            limit_price=_dec(raw.get("lmtPrice")),
            stop_price=_dec(raw.get("auxPrice")),
            filled_avg_price=_dec(raw.get("avgFillPrice")),
            filled_qty=filled if filled > 0 else None,
            raw=raw,
        )
