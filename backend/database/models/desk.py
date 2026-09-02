"""Desk persistence: opportunities, exits, audit (Stage 4 hardening)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.models.journal import JSONType, UUIDType


class OpportunityRow(Base):
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trading_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    creation_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    creation_admission_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    geometry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    legacy: Mapped[bool] = mapped_column(default=True, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_version: Mapped[int] = mapped_column(default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ExitOpportunityRow(Base):
    __tablename__ = "exit_opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    position_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OrderIntentRow(Base):
    """Durable order intent — written before the broker is ever contacted.

    `idempotency_key` is uniquely indexed: that constraint, not application
    logic, is what ultimately prevents a retry becoming a second live order.
    """

    __tablename__ = "order_intents"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="entry")
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    approval_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EntryWatchRow(Base):
    """Durable WAIT/TRIGGERED plans — survives process restart."""

    __tablename__ = "entry_watches"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    strategy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_version: Mapped[int] = mapped_column(default=0, nullable=False)
    trigger_version: Mapped[int] = mapped_column(default=0, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    converted_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    exec_timeframe: Mapped[str | None] = mapped_column(String(16), nullable=True)
    geometry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdmissionRecordRow(Base):
    __tablename__ = "admission_records"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    watch_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    evaluation_key: Mapped[str | None] = mapped_column(String(256), nullable=True, unique=True, index=True)
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geometry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_gate_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ShadowOutcomeRow(Base):
    __tablename__ = "shadow_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    shadow_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    watch_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    payload_text: Mapped[str | None] = mapped_column(Text, nullable=True)
