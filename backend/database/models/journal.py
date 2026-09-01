"""Persistence models for backtests and trade journal (Stage 2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, Uuid

from database.base import Base

# Portable JSON: JSONB on Postgres, JSON elsewhere
JSONType = JSON().with_variant(JSONB(), "postgresql")
UUIDType = Uuid(as_uuid=True).with_variant(UUID(as_uuid=True), "postgresql")


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    starting_equity: Mapped[Any] = mapped_column(Numeric(18, 4), nullable=False)
    ending_equity: Mapped[Any] = mapped_column(Numeric(18, 4), nullable=False)
    net_pnl: Mapped[Any] = mapped_column(Numeric(18, 4), nullable=False)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    avg_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_bars_held: Mapped[float | None] = mapped_column(Float, nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class TradeJournalRow(Base):
    __tablename__ = "trade_journal"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    position_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    entry: Mapped[Any] = mapped_column(Numeric(18, 8), nullable=False)
    exit: Mapped[Any] = mapped_column(Numeric(18, 8), nullable=False)
    stop: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    target: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    qty: Mapped[Any] = mapped_column(Numeric(18, 8), nullable=False)
    pnl: Mapped[Any] = mapped_column(Numeric(18, 4), nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    mfe: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    mae: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_during: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_reasons: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    exit_reasons: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trading_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmation")
    indicators_at_entry: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    assessments_at_entry: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    market_regime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_reward_planned: Mapped[float | None] = mapped_column(Float, nullable=True)
    bars_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
