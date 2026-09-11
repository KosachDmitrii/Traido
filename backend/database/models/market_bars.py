"""Durable, feed-separated source bars; never a trading-state cache."""

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class MarketBarRow(Base):
    __tablename__ = "market_bars"
    feed: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(40), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
