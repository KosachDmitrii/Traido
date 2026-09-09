"""One durable, explicitly initialized risk period per broker account."""

from typing import Any

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.models.journal import JSONType


class RiskPeriodRow(Base):
    __tablename__ = "risk_periods"

    account_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
