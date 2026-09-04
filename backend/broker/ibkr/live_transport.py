"""
Real IB Gateway / TWS transport.

Library choice: `ib_async`. `ib_insync` has been unmaintained since its author's
death and its successor is a direct community fork with the same API surface,
so the migration cost is a rename and the maintenance risk is materially lower.
It is an optional dependency — nothing here is imported unless a deployment
explicitly selects the IBKR broker with a live transport.

Honesty note: this module has never spoken to an IB Gateway. It is written
against the documented API and structured so the contract suite exercises the
adapter above it, but "implemented" is not "verified". See
`docs/architecture/vendor-lock.md`.

The boundary this file defends: no ib_async object, event, or callback escapes
it. Everything returned is a plain dict in IB vocabulary, which the adapter one
layer up translates into Traido's domain.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from broker.ibkr.config import IBKRTransportConfig
from core.enums import BrokerConnectionState

logger = logging.getLogger(__name__)


class IBKRNotConnected(RuntimeError):
    """A call was attempted while the session was not READY."""


def _load_ib_async() -> Any:
    try:
        import ib_async
    except ImportError as exc:  # pragma: no cover — depends on optional install
        raise RuntimeError(
            "ib_async is not installed. Install the optional IBKR extra "
            "(pip install 'traido[ibkr]') to use the live IBKR transport."
        ) from exc
    return ib_async


class IBKRLiveTransport:
    """Session-managing transport over ib_async.

    Connection state is explicit and readable from outside, because the
    execution service is required to refuse new exposure whenever the link is
    anything other than READY. A transport that merely throws on use would let
    the decision to trade be made before the decision to connect succeeded.
    """

    def __init__(self, config: IBKRTransportConfig | None = None) -> None:
        self._config = config or IBKRTransportConfig.from_env()
        self._state = BrokerConnectionState.DISCONNECTED
        self._ib: Any = None
        self._lock = asyncio.Lock()
        logger.info("ibkr transport configured: %s", self._config.describe())

    @property
    def config(self) -> IBKRTransportConfig:
        return self._config

    def connection_state(self) -> BrokerConnectionState:
        if self._state is BrokerConnectionState.READY and not self._is_live_session():
            # The socket died without us noticing. Report DEGRADED rather than
            # READY: the difference decides whether new orders are allowed.
            self._state = BrokerConnectionState.DEGRADED
        return self._state

    def _is_live_session(self) -> bool:
        return bool(self._ib is not None and self._ib.isConnected())

    # ── Session lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        async with self._lock:
            if self._is_live_session():
                self._state = BrokerConnectionState.READY
                return
            self._state = BrokerConnectionState.CONNECTING
            ib_async = _load_ib_async()
            self._ib = ib_async.IB()
            self._ib.errorEvent += self._on_error
            self._ib.disconnectedEvent += self._on_disconnected
            try:
                await self._ib.connectAsync(
                    host=self._config.host,
                    port=self._config.port,
                    clientId=self._config.client_id,
                    timeout=self._config.connect_timeout_sec,
                    account=self._config.account or "",
                )
            except Exception:
                self._state = BrokerConnectionState.DISCONNECTED
                raise
            self._state = BrokerConnectionState.READY
            logger.info("ibkr connected: %s", self._config.describe())

    async def disconnect(self) -> None:
        async with self._lock:
            if self._ib is not None:
                self._ib.disconnect()
            self._state = BrokerConnectionState.DISCONNECTED

    async def health_check(self) -> bool:
        """Cheap round trip. A connected socket is not the same as a live session."""
        if not self._is_live_session():
            self._state = BrokerConnectionState.DEGRADED
            return False
        try:
            await self._ib.reqCurrentTimeAsync()
        except Exception:
            logger.warning("ibkr health check failed", exc_info=True)
            self._state = BrokerConnectionState.DEGRADED
            return False
        self._state = BrokerConnectionState.READY
        return True

    async def reconnect(self) -> bool:
        """Bounded retry. Reconnecting forever silently is not a recovery strategy."""
        self._state = BrokerConnectionState.RECONNECTING
        for attempt in range(1, self._config.max_reconnect_attempts + 1):
            try:
                await self.disconnect()
                await self.connect()
            except Exception:
                logger.warning("ibkr reconnect attempt %d failed", attempt, exc_info=True)
                await asyncio.sleep(self._config.reconnect_backoff_sec)
                continue
            return True
        self._state = BrokerConnectionState.DISCONNECTED
        return False

    def _on_error(self, req_id: int, code: int, message: str, contract: Any) -> None:
        # IB reports both business rejections and connectivity failures through
        # one channel; only the connectivity codes may change session state.
        if code in {1100, 1300, 2110}:
            self._state = BrokerConnectionState.DEGRADED
        logger.warning("ibkr error %s (req %s): %s", code, req_id, message)

    def _on_disconnected(self) -> None:
        self._state = BrokerConnectionState.DISCONNECTED

    def _require_ready(self) -> Any:
        if not self._is_live_session():
            raise IBKRNotConnected(f"ibkr session is {self.connection_state().value}")
        return self._ib

    async def _ready(self) -> Any:
        """Connect lazily on first use — reconcile/health never call connect() alone."""
        if not self._is_live_session():
            await self.connect()
        return self._require_ready()

    # ── Contracts ────────────────────────────────────────────────────────────

    async def resolve_contract(self, symbol: str) -> list[dict[str, Any]]:
        ib = await self._ready()
        ib_async = _load_ib_async()
        details = await ib.reqContractDetailsAsync(ib_async.Stock(symbol.upper(), "SMART", "USD"))
        return [
            {
                "symbol": d.contract.symbol,
                "conId": d.contract.conId,
                "secType": d.contract.secType,
                "exchange": d.contract.exchange or "SMART",
                "primaryExchange": d.contract.primaryExchange,
                "currency": d.contract.currency,
            }
            for d in details
        ]

    # ── Orders ───────────────────────────────────────────────────────────────

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        ib = await self._ready()
        ib_async = _load_ib_async()
        contract = ib_async.Contract(
            conId=int(payload["conId"]),
            symbol=payload["symbol"],
            secType="STK",
            exchange="SMART",
            currency="USD",
        )
        order = ib_async.Order(
            action=payload["action"],
            orderType=payload["orderType"],
            totalQuantity=float(payload["totalQuantity"]),
            tif=payload.get("tif", "DAY"),
            # Traido's client_order_id. Survives reconnects, which is what makes
            # it usable for recovery; orderId does not.
            orderRef=payload.get("orderRef", ""),
            account=self._config.account or "",
            transmit=True,
        )
        if payload.get("lmtPrice") is not None:
            order.lmtPrice = float(payload["lmtPrice"])
        if payload.get("auxPrice") is not None:
            order.auxPrice = float(payload["auxPrice"])

        trade = ib.placeOrder(contract, order)
        await asyncio.sleep(0)  # let ib_async drain the initial status callback
        return _trade_to_dict(trade)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        ib = await self._ready()
        trade = _find_trade(ib, order_id)
        if trade is None:
            raise KeyError(f"ib has no order {order_id}")
        ib.cancelOrder(trade.order)
        await asyncio.sleep(0)
        return _trade_to_dict(trade)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        ib = await self._ready()
        trade = _find_trade(ib, order_id)
        if trade is None:
            raise KeyError(f"ib has no order {order_id}")
        return _trade_to_dict(trade)

    async def open_orders(self) -> list[dict[str, Any]]:
        ib = await self._ready()
        return [_trade_to_dict(t) for t in ib.openTrades()]

    async def orders_by_ref(self, order_ref: str) -> list[dict[str, Any]]:
        """Open *and* completed orders. Recovery needs both: a filled order is
        not an open order, and the lost-reply case is exactly the one where it
        filled while we were not looking."""
        ib = await self._ready()
        await ib.reqCompletedOrdersAsync(apiOnly=True)
        seen: dict[str, dict[str, Any]] = {}
        for trade in [*ib.trades(), *ib.openTrades()]:
            if trade.order.orderRef != order_ref:
                continue
            row = _trade_to_dict(trade)
            seen[str(row["orderId"])] = row
        return list(seen.values())

    async def executions(self) -> list[dict[str, Any]]:
        ib = await self._ready()
        fills = await ib.reqExecutionsAsync()
        return [
            {
                "orderId": f.execution.orderId,
                "permId": f.execution.permId,
                "symbol": f.contract.symbol,
                "shares": float(f.execution.shares),
                "price": float(f.execution.price),
                "time": f.execution.time.isoformat() if f.execution.time else None,
            }
            for f in fills
        ]

    async def positions(self) -> list[dict[str, Any]]:
        ib = await self._ready()
        return [
            {
                "symbol": p.contract.symbol,
                "conId": p.contract.conId,
                "position": float(p.position),
                "avgCost": float(p.avgCost),
            }
            for p in ib.positions(account=self._config.account or "")
        ]

    async def account_summary(self) -> dict[str, Any]:
        ib = await self._ready()
        rows = await ib.accountSummaryAsync(self._config.account or "All")
        return {row.tag: row.value for row in rows}


def _find_trade(ib: Any, order_id: str) -> Any:
    """Match on permId first: it is the handle that survives a reconnect."""
    for trade in ib.trades():
        if str(trade.order.permId) == str(order_id):
            return trade
    for trade in ib.trades():
        if str(trade.order.orderId) == str(order_id):
            return trade
    return None


def _trade_to_dict(trade: Any) -> dict[str, Any]:
    """Flatten an ib_async Trade into the plain IB-shaped dict the adapter maps.

    `orderId` deliberately carries permId when IB has assigned one: permId is
    globally unique and permanent, while orderId is scoped to a clientId
    session and gets reused after a restart.
    """
    status = trade.orderStatus
    perm_id = getattr(trade.order, "permId", 0)
    return {
        "orderId": str(perm_id or trade.order.orderId),
        "permId": str(perm_id) if perm_id else None,
        "sessionOrderId": str(trade.order.orderId),
        "orderRef": trade.order.orderRef or "",
        "symbol": trade.contract.symbol,
        "conId": trade.contract.conId,
        "action": trade.order.action,
        "orderType": trade.order.orderType,
        "totalQuantity": float(trade.order.totalQuantity),
        "status": status.status,
        "filled": float(status.filled or 0),
        "remaining": float(status.remaining or 0),
        "avgFillPrice": float(status.avgFillPrice) if status.avgFillPrice else None,
        "lmtPrice": getattr(trade.order, "lmtPrice", None) or None,
        "auxPrice": getattr(trade.order, "auxPrice", None) or None,
    }
