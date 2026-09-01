"""Audit trail — DB primary, optional JSONL mirror, in-memory for tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine

from core.ports import AuditPort
from core.redaction import redact_mapping
from database.models.desk import AuditEventRow
from database.session import session_factory


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
        # The audit table is the most durable thing this process writes, so it
        # is the worst place for a credential to land: a log rotates, a row does
        # not. Scrubbed centrally rather than at each caller, because the leak
        # this guards against arrived through a caller nobody thought to check.
        payload = redact_mapping(payload)
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

        if self._jsonl_path is not None:
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")


# Back-compat alias
FileAudit = DbAudit


def create_audit() -> AuditPort:
    return DbAudit()
