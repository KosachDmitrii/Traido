"""Alpaca Paper broker adapter — account + orders only.

Rate-limit aware: short TTL cache + 429 backoff so desk polling does not 500.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from broker.interface import BrokerRejection, BrokerUnreachable, assert_paper_only
from core.enums import BrokerEnvironment, OrderSide, OrderStatus, OrderType, PositionStatus
from core.schemas import OrderRecord, OrderRequest, PortfolioSnapshot, Position
from risk.kill_switch import is_kill_switch_on
from trading.pricing import format_qty

# Process-wide cache (factory creates a new broker per request)
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SEC = 4.0


def _dec(value: Any) -> Decimal | None:
    """A number the vendor may not have sent. Absent stays absent."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


class AlpacaPaperBroker:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        # Injectable so the broker contract suite can exercise this adapter's
        # own normalization instead of a stand-in.
        self._transport = transport
        assert_paper_only(self.environment)
        if "paper-api" not in self._base:
            raise RuntimeError("AlpacaPaperBroker requires paper-api base URL")

    @property
    def environment(self) -> str:
        return BrokerEnvironment.PAPER.value

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
        }

    def _cache_key(self, name: str) -> str:
        return f"{self._base}:{self._key[:8]}:{name}"

    def _cache_get(self, name: str) -> Any | None:
        hit = _CACHE.get(self._cache_key(name))
        if not hit:
            return None
        ts, value = hit
        if time.monotonic() - ts > _CACHE_TTL_SEC:
            return None
        return value

    def _cache_set(self, name: str, value: Any) -> None:
        _CACHE[self._cache_key(name)] = (time.monotonic(), value)

    def _cache_clear(self) -> None:
        prefix = f"{self._base}:{self._key[:8]}:"
        for k in list(_CACHE):
            if k.startswith(prefix):
                del _CACHE[k]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        last: httpx.Response | None = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(
                    timeout=30.0, trust_env=False, transport=self._transport
                ) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json,
                    )
            except httpx.HTTPError as exc:
                # No answer means no knowledge. Surfacing this as a distinct
                # type keeps callers from treating it as "the order was refused".
                raise BrokerUnreachable(f"alpaca transport failure: {exc}") from exc
            last = resp
            if resp.status_code != 429:
                return resp
            # Honor Retry-After when present
            wait = float(resp.headers.get("Retry-After") or (0.6 * (2**attempt)))
            await asyncio.sleep(min(wait, 5.0))
        assert last is not None
        return last

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code == 429:
            raise RuntimeError("ALPACA_RATE_LIMIT")
        resp.raise_for_status()
        return resp.json()

    async def get_portfolio(self) -> PortfolioSnapshot:
        cached: PortfolioSnapshot | None = self._cache_get("portfolio")
        if cached is not None:
            return cached.model_copy(update={"kill_switch": is_kill_switch_on()})

        try:
            acct = await self._get_json("/v2/account")
            positions = await self._get_json("/v2/positions")
        except RuntimeError as exc:
            if "ALPACA_RATE_LIMIT" in str(exc):
                stale: PortfolioSnapshot | None = self._cache_get("portfolio_stale")
                if stale is not None:
                    return stale.model_copy(update={"kill_switch": is_kill_switch_on()})
            raise

        positions = positions or []
        self._cache_set("positions_raw", positions)

        equity = Decimal(str(acct.get("equity") or "0"))
        cash = Decimal(str(acct.get("cash") or "0"))
        bp = Decimal(str(acct.get("buying_power") or "0"))
        last_equity = Decimal(str(acct.get("last_equity") or acct.get("equity") or "0"))
        day_pnl = equity - last_equity
        open_exposure = sum(
            (Decimal(str(p.get("market_value") or "0")) for p in positions),
            Decimal(0),
        )
        peak = self._load_peak_equity(equity)
        dd = float((peak - equity) / peak * 100) if peak > 0 and equity < peak else 0.0
        snap = PortfolioSnapshot(
            equity=equity,
            cash=cash,
            buying_power=bp,
            open_exposure=abs(open_exposure),
            open_positions=len(positions),
            day_pnl=day_pnl,
            week_pnl=day_pnl,
            drawdown_pct=max(0.0, dd),
            kill_switch=is_kill_switch_on(),
        )
        self._cache_set("portfolio", snap)
        self._cache_set("portfolio_stale", snap)  # longer-lived fallback (same TTL bucket ok)
        return snap

    async def list_positions(self) -> list[Position]:
        cached = self._cache_get("positions")
        if cached is not None:
            return list(cached)

        raw = self._cache_get("positions_raw")
        if raw is None:
            try:
                raw = await self._get_json("/v2/positions")
            except RuntimeError as exc:
                if "ALPACA_RATE_LIMIT" in str(exc):
                    stale = self._cache_get("positions_stale")
                    if stale is not None:
                        return list(stale)
                raise
            raw = raw or []
            self._cache_set("positions_raw", raw)

        out: list[Position] = []
        for p in raw:
            out.append(
                Position(
                    id=uuid4(),
                    symbol=str(p["symbol"]),
                    qty=Decimal(str(p["qty"])),
                    avg_entry=Decimal(str(p["avg_entry_price"])),
                    stop_price=None,
                    target_price=None,
                    status=PositionStatus.OPEN,
                    opened_at=datetime.now(UTC),
                    closed_at=None,
                    realized_pnl=None,
                    mark=_dec(p.get("current_price")),
                )
            )
        self._cache_set("positions", out)
        self._cache_set("positions_stale", out)
        return out

    async def list_open_orders(self) -> list[OrderRecord]:
        cached = self._cache_get("open_orders")
        if cached is not None:
            return list(cached)
        try:
            rows = await self._get_json("/v2/orders", params={"status": "open"})
        except RuntimeError as exc:
            if "ALPACA_RATE_LIMIT" in str(exc):
                stale = self._cache_get("open_orders_stale")
                if stale is not None:
                    return list(stale)
            raise
        orders = [self._map_order(r) for r in (rows or [])]
        self._cache_set("open_orders", orders)
        self._cache_set("open_orders_stale", orders)
        return orders

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        if is_kill_switch_on() and not request.reduces_risk:
            # Scoped to new exposure. The switch used to refuse every order
            # here, which also refused the protective stop reconciliation was
            # trying to install and the emergency close that is the last way
            # out — so halting the desk disarmed it. Pressing the switch means
            # "stop taking on risk", never "stop defending what is open".
            raise RuntimeError("KILL_SWITCH blocks new exposure")
        payload: dict[str, Any] = {
            "symbol": request.symbol.upper(),
            "qty": format_qty(request.qty),
            "side": request.side.value,
            "type": request.order_type.value,
            "time_in_force": "day" if request.order_type != OrderType.STOP else "gtc",
            "client_order_id": request.client_order_id,
        }
        if request.limit_price is not None:
            payload["limit_price"] = str(request.limit_price)
        if request.stop_price is not None:
            payload["stop_price"] = str(request.stop_price)

        resp = await self._request("POST", "/v2/orders", json=payload)
        self._cache_clear()
        if resp.status_code >= 400:
            raise BrokerRejection(f"Alpaca order rejected: {resp.status_code} {resp.text[:300]}")
        return self._map_order(resp.json())

    async def cancel_order(self, broker_order_id: str) -> OrderRecord:
        resp = await self._request("DELETE", f"/v2/orders/{broker_order_id}")
        self._cache_clear()
        if resp.status_code >= 400 and resp.status_code != 204:
            raise RuntimeError(f"Alpaca cancel failed: {resp.status_code} {resp.text[:200]}")
        if resp.status_code == 204:
            return OrderRecord(
                id=uuid4(),
                client_order_id="",
                broker_order_id=broker_order_id,
                symbol="",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                qty=Decimal(0),
                status=OrderStatus.CANCELED,
            )
        return self._map_order(resp.json())

    async def get_order(self, broker_order_id: str) -> OrderRecord:
        # No cache — fill polling needs fresh status; still retry 429
        resp = await self._request("GET", f"/v2/orders/{broker_order_id}", retries=4)
        if resp.status_code == 429:
            raise RuntimeError(f"ALPACA_RATE_LIMIT:order:{broker_order_id}")
        if resp.status_code >= 400:
            # Failing to read an order is never proof the order is gone, so this
            # is ambiguity rather than a rejection.
            raise BrokerUnreachable(
                f"Alpaca order read failed: {resp.status_code} {resp.text[:200]}"
            )
        return self._map_order(resp.json())

    async def find_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        if not client_order_id:
            return None
        resp = await self._request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise BrokerUnreachable(
                f"Alpaca client-id lookup failed: {resp.status_code} {resp.text[:200]}"
            )
        return self._map_order(resp.json())

    def _map_order(self, raw: dict[str, Any]) -> OrderRecord:
        status_map = {
            "new": OrderStatus.ACCEPTED,
            "accepted": OrderStatus.ACCEPTED,
            "pending_new": OrderStatus.SUBMITTED,
            "filled": OrderStatus.FILLED,
            "partially_filled": OrderStatus.PARTIAL,
            "canceled": OrderStatus.CANCELED,
            "cancelled": OrderStatus.CANCELED,
            "rejected": OrderStatus.REJECTED,
            "expired": OrderStatus.EXPIRED,
            "done_for_day": OrderStatus.EXPIRED,
        }
        side = OrderSide.BUY if raw.get("side") == "buy" else OrderSide.SELL
        otype = {
            "market": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop": OrderType.STOP,
            "stop_limit": OrderType.STOP_LIMIT,
        }.get(str(raw.get("type") or "market"), OrderType.MARKET)
        return OrderRecord(
            id=uuid4(),
            client_order_id=str(raw.get("client_order_id") or ""),
            broker_order_id=str(raw.get("id") or ""),
            symbol=str(raw.get("symbol") or ""),
            side=side,
            order_type=otype,
            qty=Decimal(str(raw.get("qty") or "0")),
            status=status_map.get(str(raw.get("status") or ""), OrderStatus.SUBMITTED),
            limit_price=Decimal(str(raw["limit_price"])) if raw.get("limit_price") else None,
            stop_price=Decimal(str(raw["stop_price"])) if raw.get("stop_price") else None,
            filled_avg_price=(
                Decimal(str(raw["filled_avg_price"])) if raw.get("filled_avg_price") else None
            ),
            filled_qty=(
                Decimal(str(raw["filled_qty"]))
                if raw.get("filled_qty")
                else (Decimal(str(raw["qty"])) if str(raw.get("status")) == "filled" else None)
            ),
            raw=raw,
        )

    def _load_peak_equity(self, equity: Decimal) -> Decimal:
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "data" / "peak_equity.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        peak = equity
        if path.exists():
            try:
                peak = max(equity, Decimal(path.read_text().strip() or "0"))
            except Exception:  # noqa: BLE001
                peak = equity
        else:
            peak = equity
        if equity >= peak:
            peak = equity
            path.write_text(str(peak))
        return peak
