"""
Fake broker back-ends for the lifecycle contract suite.

Each fake speaks its vendor's native dialect — Alpaca's REST JSON, IB's order
dicts — so the suite exercises each adapter's real normalization code rather
than a shared stand-in that would prove nothing about either.

Both expose the same knobs, which is what makes one suite runnable against two
brokers: fill ratio, outright rejection, and a broker that has no record of an
order we believe we sent.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx

from broker.alpaca import AlpacaPaperBroker
from broker.ibkr import FakeIBKRTransport, IBKRBroker
from core.ports import BrokerPort


class FakeAlpacaBackend:
    """Minimal in-memory Alpaca REST API."""

    def __init__(
        self,
        *,
        equity: float = 100_000.0,
        fill_ratio: float = 1.0,
        reject: bool = False,
        forget_orders: bool = False,
    ) -> None:
        self.equity = equity
        self.fill_ratio = fill_ratio
        self.reject = reject
        self.forget_orders = forget_orders
        self.orders: dict[str, dict[str, Any]] = {}
        self.holdings: dict[str, dict[str, Any]] = {}
        self.submit_count = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/v2/account":
            invested = sum(h["qty"] * h["avg_entry_price"] for h in self.holdings.values())
            return httpx.Response(
                200,
                json={
                    "equity": str(self.equity),
                    "cash": str(self.equity - invested),
                    "buying_power": str(self.equity - invested),
                    "last_equity": str(self.equity),
                },
            )

        if path == "/v2/positions":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": h["symbol"],
                        "qty": str(h["qty"]),
                        "avg_entry_price": str(h["avg_entry_price"]),
                        "unrealized_pl": "0",
                    }
                    for h in self.holdings.values()
                ],
            )

        if path == "/v2/orders" and method == "POST":
            return self._create(json.loads(request.content or b"{}"))

        if path == "/v2/orders" and method == "GET":
            open_states = {"new", "accepted", "partially_filled", "pending_new"}
            return httpx.Response(
                200,
                json=[o for o in self.orders.values() if o["status"] in open_states],
            )

        if path == "/v2/orders:by_client_order_id":
            wanted = request.url.params.get("client_order_id")
            match = next((o for o in self.orders.values() if o["client_order_id"] == wanted), None)
            if match is None:
                return httpx.Response(404, json={"message": "order not found"})
            return httpx.Response(200, json=match)

        if path.startswith("/v2/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            record = self.orders.get(order_id)
            if record is None:
                return httpx.Response(404, json={"message": "order not found"})
            if method == "DELETE":
                if record["status"] != "filled":
                    record["status"] = "canceled"
                return httpx.Response(200, json=record)
            return httpx.Response(200, json=record)

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})

    def _create(self, payload: dict[str, Any]) -> httpx.Response:
        self.submit_count += 1
        if self.reject:
            return httpx.Response(422, json={"message": "sub-penny increment"})

        qty = float(payload["qty"])
        # A protective stop rests until its trigger price trades; treating it as
        # instantly filled would hide the very gap reconciliation looks for.
        ratio = 0.0 if payload["type"] == "stop" else self.fill_ratio
        filled = round(qty * ratio, 8)
        price = float(payload.get("limit_price") or payload.get("stop_price") or 100.0)

        if filled >= qty:
            status = "filled"
        elif filled > 0:
            status = "partially_filled"
        else:
            status = "accepted"

        order_id = str(uuid4())
        record = {
            "id": order_id,
            "client_order_id": payload.get("client_order_id", ""),
            "symbol": payload["symbol"],
            "side": payload["side"],
            "type": payload["type"],
            "qty": str(qty),
            "status": status,
            "limit_price": payload.get("limit_price"),
            "stop_price": payload.get("stop_price"),
            "filled_qty": str(filled),
            "filled_avg_price": str(price) if filled > 0 else None,
        }
        if not self.forget_orders:
            self.orders[order_id] = record
        if filled > 0:
            self._apply_fill(payload["symbol"], payload["side"], filled, price)
        return httpx.Response(200, json=record)

    def _apply_fill(self, symbol: str, side: str, qty: float, price: float) -> None:
        held = self.holdings.get(symbol)
        if side == "buy":
            if held is None:
                self.holdings[symbol] = {
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry_price": price,
                }
            else:
                total = held["qty"] + qty
                held["avg_entry_price"] = (
                    held["avg_entry_price"] * held["qty"] + price * qty
                ) / total
                held["qty"] = total
            return
        if held is None:
            return
        held["qty"] -= qty
        if held["qty"] <= 0:
            del self.holdings[symbol]


def alpaca_adapter(**kwargs: Any) -> tuple[BrokerPort, FakeAlpacaBackend]:
    backend = FakeAlpacaBackend(**kwargs)
    broker = AlpacaPaperBroker(
        api_key="contract-key",
        api_secret="contract-secret",
        base_url="https://paper-api.example.test",
        transport=backend.transport(),
    )
    return broker, backend


def ibkr_adapter(**kwargs: Any) -> tuple[BrokerPort, FakeIBKRTransport]:
    transport = FakeIBKRTransport(**kwargs)
    return IBKRBroker(transport), transport
