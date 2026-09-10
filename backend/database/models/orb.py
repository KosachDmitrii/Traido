"""Immutable daily ORB selections and their observation state."""

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class OrbSessionRow(Base):
    __tablename__ = "orb_sessions"
    session: Mapped[str] = mapped_column(String(10), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
