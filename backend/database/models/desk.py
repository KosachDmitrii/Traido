"""Desk persistence: opportunities, exits, audit (Stage 4 hardening)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.models.journal import JSONType, UUIDType


class OpportunityRow(Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        Index("ix_opportunities_creation_admission", "creation_admission_record_id"),
        Index("ix_opportunities_approval_admission", "approval_admission_record_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trading_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    creation_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    creation_admission_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("admission_records.id", name="fk_opportunities_approval_admission"),
        nullable=True,
    )
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
    __table_args__ = (
        Index("ix_order_intents_approval_admission", "approval_admission_record_id"),
        Index(
            "uq_entry_intent_opportunity_request",
            "opportunity_id",
            "request_id",
            unique=True,
            sqlite_where=text("purpose = 'entry' AND request_id IS NOT NULL"),
            postgresql_where=text("purpose = 'entry' AND request_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (approval_admission_record_id IS NOT NULL)",
            name="ck_entry_intent_has_approval_admission",
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (geometry_hash IS NOT NULL)",
            name="ck_entry_intent_has_geometry_hash",
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (request_id IS NOT NULL)",
            name="ck_entry_intent_has_request_id",
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (request_fingerprint IS NOT NULL)",
            name="ck_entry_intent_has_request_fingerprint",
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (broker IS NOT NULL)",
            name="ck_entry_intent_has_broker",
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (broker_account_id IS NOT NULL)",
            name="ck_entry_intent_has_broker_account_id",
        ),
        CheckConstraint(
            "(purpose != 'entry') OR (broker_environment = 'paper')",
            name="ck_entry_intent_paper_environment",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="entry")
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    broker_environment: Mapped[str | None] = mapped_column(
        String(16), nullable=True, server_default="paper"
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    approval_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("admission_records.id", name="fk_order_intents_approval_admission"),
        nullable=True,
    )
    geometry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EntryWatchRow(Base):
    """Durable WAIT/TRIGGERED plans — survives process restart."""

    __tablename__ = "entry_watches"
    __table_args__ = (
        Index("ix_entry_watches_last_admission", "last_admission_record_id"),
        Index(
            "ix_entry_watches_converted_opp",
            "converted_opportunity_id",
            unique=True,
            sqlite_where=text("converted_opportunity_id IS NOT NULL"),
            postgresql_where=text("converted_opportunity_id IS NOT NULL"),
        ),
        Index(
            "ix_entry_watches_active_symbol_strategy",
            "symbol",
            "strategy_version",
            unique=True,
            sqlite_where=text(
                "status IN ('waiting','triggered','revalidating','admitted','converting')"
            ),
            postgresql_where=text(
                "status IN ('waiting','triggered','revalidating','admitted','converting')"
            ),
        ),
    )

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
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_admission_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    converted_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    exec_timeframe: Mapped[str | None] = mapped_column(String(16), nullable=True)
    geometry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdmissionRecordRow(Base):
    __tablename__ = "admission_records"
    __table_args__ = (
        Index(
            "ix_admission_records_evaluation_key",
            "evaluation_key",
            unique=True,
            sqlite_where=text("evaluation_key IS NOT NULL"),
            postgresql_where=text("evaluation_key IS NOT NULL"),
        ),
        Index("ix_admission_records_request_fingerprint", "request_fingerprint"),
        CheckConstraint(
            "(phase != 'approval') OR (request_id IS NOT NULL)",
            name="ck_approval_admission_has_request_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    watch_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    evaluation_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geometry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_gate_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class ShadowOutcomeRow(Base):
    __tablename__ = "shadow_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    shadow_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    watch_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ExternalPositionIncidentRow(Base):
    """Orphan/external broker exposure — never an OrderIntent or admission."""

    __tablename__ = "external_position_incidents"
    __table_args__ = (
        Index(
            "uq_open_external_position_incident",
            "broker",
            "account_id",
            "symbol",
            unique=True,
            sqlite_where=text("resolution = 'open'"),
            postgresql_where=text("resolution = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    broker_perm_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="open")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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


class ArchivedEntryIntentRow(Base):
    """Quarantine for legacy entry intents that lacked admission/geometry."""

    __tablename__ = "archived_entry_intents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    archive_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class ArchivedActivityEventRow(Base):
    """Archive of pre-0011/0012 activity_events rows (no silent DROP)."""

    __tablename__ = "archived_activity_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    archive_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
