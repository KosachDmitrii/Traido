"""In-memory paper broker for tests — fill-aware, sell closes positions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from broker.interface import assert_paper_only
from core.enums import BrokerEnvironment, OrderStatus, OrderType, PositionStatus
from core.schemas import OrderRecord, OrderRequest, PortfolioSnapshot, Position
from risk.kill_switch import is_kill_switch_on


class MockPaperBroker:
    def __init__(self, equity: Decimal = Decimal(100000)) -> None:
        self._equity = equity
        self._cash = equity
        self._peak_equity = equity
        self.orders: list[OrderRecord] = []
        self.positions: list[Position] = []
        self.marks: dict[str, Decimal] = {}  # optional mark prices for market fills
        self.account_id: str = "mock-paper-account"
        assert_paper_only(self.environment)

    @property
    def environment(self) -> str:
        return BrokerEnvironment.PAPER.value

    def _mark(self, symbol: str, fallback: Decimal) -> Decimal:
        return self.marks.get(symbol.upper(), fallback)

    async def get_portfolio(self) -> PortfolioSnapshot:
        exposure = sum((p.qty * p.avg_entry for p in self.positions), Decimal(0))
        mtm = sum(
            (p.qty * self._mark(p.symbol, p.avg_entry) for p in self.positions),
            Decimal(0),
        )
        equity = self._cash + mtm
        self._equity = equity
        self._peak_equity = max(self._peak_equity, equity)
        dd = 0.0
        if self._peak_equity > 0:
            dd = float((self._peak_equity - equity) / self._peak_equity * 100)
        return PortfolioSnapshot(
            equity=equity,
            cash=self._cash,
            buying_power=self._cash,
            open_exposure=exposure,
            open_positions=len(self.positions),
            day_pnl=equity - Decimal(100000),  # vs starting for mock
            week_pnl=equity - Decimal(100000),
            drawdown_pct=max(0.0, dd),
            kill_switch=is_kill_switch_on(),
        )

    async def list_positions(self) -> list[Position]:
        return list(self.positions)

    async def list_open_orders(self) -> list[OrderRecord]:
        return [
            o
            for o in self.orders
            if o.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
        ]

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        if is_kill_switch_on() and not request.reduces_risk:
            raise RuntimeError("KILL_SWITCH blocks new exposure")

        symbol = request.symbol.upper()
        fill_price = request.limit_price or request.stop_price
        if fill_price is None or fill_price <= 0:
            # Market: use mark or existing position avg
            pos = next((p for p in self.positions if p.symbol == symbol), None)
            fill_price = self._mark(symbol, pos.avg_entry if pos else Decimal(100))

        # Resting stop (not filled until mark breaches) — for lifecycle tests we auto-accept as open
        if request.order_type == OrderType.STOP and request.side.value == "sell":
            record = OrderRecord(
                id=uuid4(),
                client_order_id=request.client_order_id,
                broker_order_id=str(uuid4()),
                symbol=symbol,
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                status=OrderStatus.ACCEPTED,
                limit_price=request.limit_price,
                stop_price=request.stop_price,
                filled_avg_price=None,
                filled_qty=None,
                raw={"mock": True, "reason": request.reason, "resting_stop": True},
            )
            self.orders.append(record)
            # Attach stop to position metadata
            for i, p in enumerate(self.positions):
                if p.symbol == symbol:
                    self.positions[i] = p.model_copy(update={"stop_price": request.stop_price})
            return record

        if request.side.value == "buy":
            cost = request.qty * fill_price
            if cost > self._cash:
                raise RuntimeError("Insufficient cash in mock broker")
            self._cash -= cost
            existing = next((p for p in self.positions if p.symbol == symbol), None)
            if existing:
                new_qty = existing.qty + request.qty
                existing.avg_entry = (
                    existing.avg_entry * existing.qty + fill_price * request.qty
                ) / new_qty
                existing.qty = new_qty
            else:
                self.positions.append(
                    Position(
                        id=uuid4(),
                        symbol=symbol,
                        qty=request.qty,
                        avg_entry=fill_price,
                        stop_price=None,
                        target_price=None,
                        status=PositionStatus.OPEN,
                        opened_at=datetime.now(UTC),
                    )
                )
        else:
            # SELL — close / reduce
            pos = next((p for p in self.positions if p.symbol == symbol), None)
            if pos is None:
                raise RuntimeError(f"No position to sell for {symbol}")
            sell_qty = min(request.qty, pos.qty)
            self._cash += sell_qty * fill_price
            pos.qty -= sell_qty
            if pos.qty <= 0:
                self.positions = [p for p in self.positions if p.symbol != symbol]

        record = OrderRecord(
            id=uuid4(),
            client_order_id=request.client_order_id,
            broker_order_id=str(uuid4()),
            symbol=symbol,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            status=OrderStatus.FILLED,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            filled_avg_price=fill_price,
            filled_qty=request.qty,
            raw={"mock": True, "reason": request.reason},
        )
        self.orders.append(record)
        return record

    async def cancel_order(self, broker_order_id: str) -> OrderRecord:
        for o in self.orders:
            if o.broker_order_id == broker_order_id:
                o.status = OrderStatus.CANCELED
                return o
        raise RuntimeError("order not found")

    async def get_order(self, broker_order_id: str) -> OrderRecord:
        for o in self.orders:
            if o.broker_order_id == broker_order_id:
                return o
        raise RuntimeError("order not found")

    async def find_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        if not client_order_id:
            return None
        return next((o for o in self.orders if o.client_order_id == client_order_id), None)
