"""
Port interfaces — capital path boundaries.

Agents depend on these Protocols; they must never import concrete broker SDKs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from core.enums import BrokerConnectionState, Timeframe
from core.schemas import (
    Bar,
    OrderRecord,
    OrderRequest,
    PortfolioSnapshot,
    Position,
    Quote,
    Snapshot,
)


@runtime_checkable
class MarketDataPort(Protocol):
    async def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]: ...

    async def get_last_price(self, symbol: str) -> float: ...


@runtime_checkable
class BatchMarketDataPort(Protocol):
    """Multi-symbol reads, for the stages that run over the whole universe.

    Kept separate from `MarketDataPort` for the same reason `QuotePort` is: a
    provider that can only serve one symbol at a time must not be able to
    satisfy a batch requirement merely by being a market-data provider. Callers
    check for this protocol and fall back to a bounded per-symbol loop when it
    is absent, so a provider without batching is slower and never wrong.
    """

    async def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]: ...

    async def get_daily_bars_batch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[Bar]]: ...


@runtime_checkable
class QuotePort(Protocol):
    """Live top of book.

    Kept separate from `MarketDataPort` because serving bars and serving quotes
    are different capabilities: a provider that has only bars must not be able
    to satisfy a spread check just by being a market-data provider.
    """

    async def get_quote(self, symbol: str) -> Quote | None: ...


@runtime_checkable
class ConnectionAwareBroker(Protocol):
    """Implemented by session-based adapters such as IBKR.

    Stateless REST adapters do not implement it and are treated as READY —
    they have no session to lose, and a failed call raises rather than lying.
    """

    def connection_state(self) -> BrokerConnectionState: ...


@runtime_checkable
class BrokerPort(Protocol):
    """Execution + account. V1 implementations MUST be paper-only."""

    @property
    def environment(self) -> str:
        """Must return 'paper' in V1."""
        ...

    async def get_portfolio(self) -> PortfolioSnapshot: ...

    async def list_positions(self) -> list[Position]: ...

    async def list_open_orders(self) -> list[OrderRecord]: ...

    async def place_order(self, request: OrderRequest) -> OrderRecord: ...

    async def cancel_order(self, broker_order_id: str) -> OrderRecord: ...

    async def get_order(self, broker_order_id: str) -> OrderRecord: ...

    async def find_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        """Look up an order by the id *we* assigned, open or closed.

        Required for recovery: when a submit reply is lost we know only our own
        client id, and the open-order book cannot answer, because an order that
        filled in the meantime is no longer open.
        """
        ...


@runtime_checkable
class LLMPort(Protocol):
    """Structured generation only — caller validates with Pydantic."""

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]: ...


@runtime_checkable
class AuditPort(Protocol):
    async def append(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        pipeline_run_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> None: ...
