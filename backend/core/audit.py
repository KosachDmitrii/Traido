"""Audit trail — DB primary, optional JSONL mirror, in-memory for tests."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult, Engine

from core.ports import AuditPort
from core.redaction import redact_mapping
from database.models.desk import AuditEventRow
from database.session import session_factory

logger = logging.getLogger(__name__)

ACTIVITY_LOG_EVENT = "activity.log"


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        pipeline_run_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        payload = redact_mapping(payload)
        self.events.append(
            {
                "id": str(uuid4()),
                "created_at": datetime.now(UTC).isoformat(),
                "event_type": event_type,
                "actor": actor,
                "pipeline_run_id": str(pipeline_run_id) if pipeline_run_id else None,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": payload,
            }
        )


class DbAudit:
    """Append-only audit_events table (+ optional JSONL mirror)."""

    def __init__(self, engine: Engine | None = None, *, mirror_jsonl: bool = True) -> None:
        self._engine = engine
        self._memory = InMemoryAudit()
        self._mirror_jsonl = mirror_jsonl
        self._jsonl_path: Path | None = None
        if mirror_jsonl:
            root = Path(__file__).resolve().parents[1] / "data" / "audit"
            root.mkdir(parents=True, exist_ok=True)
            self._jsonl_path = root / "events.jsonl"

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._memory.events

    def _write_row(
        self,
        *,
        event_id: UUID,
        created: datetime,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        pipeline_run_id: UUID | None,
        entity_type: str | None,
        entity_id: str | None,
        remember: bool,
        mirror: bool,
    ) -> dict[str, Any]:
        record = {
            "id": str(event_id),
            "created_at": created.isoformat(),
            "event_type": event_type,
            "actor": actor,
            "pipeline_run_id": str(pipeline_run_id) if pipeline_run_id else None,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
        }
        if remember:
            self._memory.events.append(record)

        SessionLocal = session_factory(self._engine)
        with SessionLocal() as session:
            session.add(
                AuditEventRow(
                    id=event_id,
                    created_at=created,
                    event_type=event_type,
                    actor=actor,
                    pipeline_run_id=pipeline_run_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload,
                    payload_text=json.dumps(payload, default=str),
                )
            )
            session.commit()

        if mirror and self._jsonl_path is not None:
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        return record

    async def append(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        pipeline_run_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        event_id = uuid4()
        created = datetime.now(UTC)
        payload = redact_mapping(payload)
        self._write_row(
            event_id=event_id,
            created=created,
            event_type=event_type,
            actor=actor,
            payload=payload,
            pipeline_run_id=pipeline_run_id,
            entity_type=entity_type,
            entity_id=entity_id,
            remember=True,
            mirror=True,
        )

    def append_sync(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        pipeline_run_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        remember: bool = False,
    ) -> None:
        """Sync append for hot paths (activity board). DB is durable; memory optional."""
        event_id = uuid4()
        created = datetime.now(UTC)
        payload = redact_mapping(payload)
        try:
            self._write_row(
                event_id=event_id,
                created=created,
                event_type=event_type,
                actor=actor,
                payload=payload,
                pipeline_run_id=pipeline_run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                remember=remember,
                mirror=True,
            )
        except Exception:
            logger.exception("audit append_sync failed for %s", event_type)

    def list_events(
        self,
        *,
        limit: int = 500,
        before: datetime | None = None,
        event_type: str | None = ACTIVITY_LOG_EVENT,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 2000))
        SessionLocal = session_factory(self._engine)
        with SessionLocal() as session:
            stmt = select(AuditEventRow).order_by(AuditEventRow.created_at.desc()).limit(limit)
            if event_type is not None:
                stmt = stmt.where(AuditEventRow.event_type == event_type)
            if actor is not None:
                stmt = stmt.where(AuditEventRow.actor == actor)
            if before is not None:
                stmt = stmt.where(AuditEventRow.created_at < before)
            rows = session.scalars(stmt).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            out.append(
                {
                    "id": str(row.id),
                    "ts": row.created_at.isoformat().replace("+00:00", "Z"),
                    "event_type": row.event_type,
                    "agent": row.actor,
                    "message": str(payload.get("message") or ""),
                    "symbol": payload.get("symbol"),
                    "level": str(payload.get("level") or "info"),
                    "payload": payload,
                }
            )
        return out

    def prune_before(self, cutoff: datetime) -> int:
        SessionLocal = session_factory(self._engine)
        with SessionLocal() as session:
            cursor = cast(
                CursorResult[Any],
                session.execute(delete(AuditEventRow).where(AuditEventRow.created_at < cutoff)),
            )
            session.commit()
            return int(cursor.rowcount or 0)


# Back-compat alias
FileAudit = DbAudit


def create_audit() -> AuditPort:
    return DbAudit()
