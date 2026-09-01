"""Open positions ledger — Traido-tracked paper positions (Stage 5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Numeric, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.models.journal import JSONType, UUIDType


class OpenPositionRow(Base):
    """Positions opened via Traido ExecutionService (not raw broker dump)."""

    __tablename__ = "open_positions"

    __table_args__ = (
        Index(
            "ux_open_positions_one_open_per_symbol",
            "symbol",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )
    """One open position per symbol, enforced by the database.

    The rule was already there in Python — `PositionLedger.open_from_opportunity`
    refuses a second row under a `threading.Lock`, and the execution service
    refuses the entry before that. Both hold within one process and neither
    holds across two, and a book with two open rows for one symbol can never
    agree with a broker reporting a single net position: each row carries its
    own stop, for shares the other row also claims.

    Partial rather than plain, because closed rows accumulate for the same
    symbol and must not collide. Both dialects in use support the syntax, with
    different keywords.
    """

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    qty: Mapped[Any] = mapped_column(Numeric(18, 8), nullable=False)
    avg_entry: Mapped[Any] = mapped_column(Numeric(18, 8), nullable=False)
    stop_price: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    target_price: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmation")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    entry_reasons: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    broker_entry_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
