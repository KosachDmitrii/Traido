"""Durable vendor classification evidence; never an entry authorization."""

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class SectorClassificationRow(Base):
    __tablename__ = "sector_classifications"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
