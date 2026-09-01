"""
Transport boundary for the IBKR adapter.

The adapter is written against this Protocol rather than against a specific IB
client, which is what lets the lifecycle contract suite exercise its mapping
logic today, before any credentials exist. Two implementations ship:

- `FakeIBKRTransport`, an in-memory order book used by the contract suite.
- `UnconfiguredIBKRTransport`, which refuses loudly rather than pretending to
  be connected.

The real transport (ib_insync / IB Gateway) is deliberately absent: there is no
IBKR paper connectivity in this environment, and a stub that silently behaved
like a broker would be worse than no adapter at all.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from core.enums import BrokerConnectionState


class IBKRTransport(Protocol):
    """Raw IB-shaped calls. Everything here speaks IB vocabulary, not Traido's."""

    async def resolve_contract(self, symbol: str) -> list[dict[str, Any]]:
        """Candidate contracts for a ticker. May legitimately return several."""
        ...

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    async def get_order(self, order_id: str) -> dict[str, Any]: ...

    async def open_orders(self) -> list[dict[str, Any]]: ...

    async def orders_by_ref(self, order_ref: str) -> list[dict[str, Any]]:
        """Open *and* completed orders carrying this reference — recovery needs both."""
        ...

    async def positions(self) -> list[dict[str, Any]]: ...

    async def account_summary(self) -> dict[str, Any]: ...


class IBKRNotConfigured(RuntimeError):
    """Raised when the adapter is used without a working IB connection."""


class UnconfiguredIBKRTransport:
    """Default transport: fails fast instead of faking a broker."""

    _MESSAGE = (
        "IBKR transport is not configured. Traido ships the IBKR adapter and its "
        "contract tests, but live/paper IB connectivity is not wired up yet. "
        "Set TRAIDO_BROKER=alpaca until an IB Gateway session is available."
    )

    def connection_state(self) -> BrokerConnectionState:
        return BrokerConnectionState.DISCONNECTED

    async def resolve_contract(self, symbol: str) -> list[dict[str, Any]]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def open_orders(self) -> list[dict[str, Any]]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def orders_by_ref(self, order_ref: str) -> list[dict[str, Any]]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def positions(self) -> list[dict[str, Any]]:
        raise IBKRNotConfigured(self._MESSAGE)

    async def account_summary(self) -> dict[str, Any]:
        raise IBKRNotConfigured(self._MESSAGE)


class FakeIBKRTransport:
    """In-memory IB order book with the failure modes we need to test against.

    IB vocabulary on purpose: `PreSubmitted`, `Filled`, `ApiCancelled`, and a
    `remaining` field. If the adapter's mapping is wrong, the contract suite
    fails here rather than in production.
    """

    def __init__(
        self,
        *,
        equity: float = 100_000.0,
        fill_ratio: float = 1.0,
        reject: bool = False,
        unreachable: bool = False,
        forget_orders: bool = False,
        contracts: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.equity = equity
        self.fill_ratio = fill_ratio
        self.reject = reject
        self.unreachable = unreachable
        self.forget_orders = forget_orders
        """Simulates a broker that has no trace of an order we believe we sent."""
        self.orders: dict[str, dict[str, Any]] = {}
        self.holdings: dict[str, dict[str, Any]] = {}
        self.submit_count = 0
        self.contracts = contracts if contracts is not None else {}
        self._perm_seq = 1_000_000

    def connection_state(self) -> BrokerConnectionState:
        return (
            BrokerConnectionState.DISCONNECTED if self.unreachable else BrokerConnectionState.READY
        )

    async def resolve_contract(self, symbol: str) -> list[dict[str, Any]]:
        self._guard()
        ticker = symbol.upper()
        if ticker in self.contracts:
            return self.contracts[ticker]
        return [
            {
                "symbol": ticker,
                "conId": 265598 + len(ticker),
                "secType": "STK",
                "exchange": "SMART",
                "primaryExchange": "NASDAQ",
                "currency": "USD",
            }
        ]

    def _guard(self) -> None:
        if self.unreachable:
            raise ConnectionError("ib gateway unreachable")

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        self.submit_count += 1
        if self.reject:
            raise ValueError("ib error 201: order rejected - insufficient margin")

        qty = float(payload["totalQuantity"])
        # A protective stop rests until its trigger price trades; treating it as
        # instantly filled would hide the very gap reconciliation looks for.
        ratio = 0.0 if payload["orderType"] in {"STP", "STP LMT"} else self.fill_ratio
        filled = round(qty * ratio, 8)
        remaining = round(qty - filled, 8)
        price = float(payload.get("lmtPrice") or payload.get("auxPrice") or 100.0)

        if remaining <= 0:
            status = "Filled"
        elif filled > 0:
            status = "Submitted"
        else:
            status = "PreSubmitted"

        order_id = str(uuid4())
        self._perm_seq += 1
        record = {
            "orderId": order_id,
            # IB's permanent, account-wide order id. The one handle that still
            # means something after a reconnect or a clientId change.
            "permId": str(self._perm_seq),
            "orderRef": payload.get("orderRef", ""),
            "symbol": payload["symbol"],
            "action": payload["action"],
            "orderType": payload["orderType"],
            "totalQuantity": qty,
            "status": status,
            "filled": filled,
            "remaining": remaining,
            "avgFillPrice": price if filled > 0 else None,
            "lmtPrice": payload.get("lmtPrice"),
            "auxPrice": payload.get("auxPrice"),
        }
        if not self.forget_orders:
            self.orders[order_id] = record
        if filled > 0:
            self._apply_fill(payload["symbol"], payload["action"], filled, price)
        return record

    def _apply_fill(self, symbol: str, action: str, qty: float, price: float) -> None:
        held = self.holdings.get(symbol)
        if action == "BUY":
            if held is None:
                self.holdings[symbol] = {"symbol": symbol, "position": qty, "avgCost": price}
            else:
                total = held["position"] + qty
                held["avgCost"] = (held["avgCost"] * held["position"] + price * qty) / total
                held["position"] = total
            return
        if held is None:
            return
        held["position"] -= qty
        if held["position"] <= 0:
            del self.holdings[symbol]

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._guard()
        record = self.orders.get(order_id)
        if record is None:
            raise KeyError(f"ib error 135: unknown order {order_id}")
        if record["status"] != "Filled":
            # IB keeps the fill on a partially executed order it cancels.
            record["status"] = "ApiCancelled" if record["filled"] else "Cancelled"
            record["remaining"] = 0.0
        return record

    async def get_order(self, order_id: str) -> dict[str, Any]:
        self._guard()
        record = self.orders.get(order_id)
        if record is None:
            raise KeyError(f"ib error 135: unknown order {order_id}")
        return record

    async def open_orders(self) -> list[dict[str, Any]]:
        self._guard()
        closed = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
        return [o for o in self.orders.values() if o["status"] not in closed]

    async def orders_by_ref(self, order_ref: str) -> list[dict[str, Any]]:
        self._guard()
        return [o for o in self.orders.values() if o.get("orderRef") == order_ref]

    async def positions(self) -> list[dict[str, Any]]:
        self._guard()
        return list(self.holdings.values())

    async def account_summary(self) -> dict[str, Any]:
        self._guard()
        invested = sum(h["position"] * h["avgCost"] for h in self.holdings.values())
        return {
            "NetLiquidation": self.equity,
            "TotalCashValue": self.equity - invested,
            "BuyingPower": self.equity - invested,
        }
