"""Observation-only evidence, never consulted by trading gates."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.models.journal import JSONType


class MonitoringSampleRow(Base):
    __tablename__ = "monitoring_samples"

    # One sample per UTC minute; restarting cannot double-count observations.
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    session: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)


class MonitoringReportRow(Base):
    __tablename__ = "monitoring_reports"

    session: Mapped[str] = mapped_column(String(10), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
