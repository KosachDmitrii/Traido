"""Activity logs persist to audit_events and expire after retention window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.activity import BOARD, bind_activity_audit
from core.audit import ACTIVITY_LOG_EVENT, DbAudit
from core.log_retention import prune_audit_events
from database.models.desk import AuditEventRow
from database.session import session_factory


@pytest.fixture
def audit_engine(tmp_path, monkeypatch):
    db_path = tmp_path / "audit_test.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TRAIDO_JOURNAL_DATABASE_URL", url)
    from database.session import init_db

    init_db()
    return DbAudit(mirror_jsonl=False)


def test_board_log_persists_to_audit(audit_engine: DbAudit) -> None:
    bind_activity_audit(audit_engine)
    BOARD.log("scanner", "cycle complete", symbol="FCX", level="info")

    rows = audit_engine.list_events(limit=10)
    assert len(rows) == 1
    assert rows[0]["agent"] == "scanner"
    assert rows[0]["message"] == "cycle complete"
    assert rows[0]["symbol"] == "FCX"
    assert rows[0]["level"] == "info"


def test_prune_removes_old_audit_rows(audit_engine: DbAudit) -> None:
    bind_activity_audit(audit_engine)
    old = datetime.now(UTC) - timedelta(days=40)
    SessionLocal = session_factory(audit_engine._engine)
    with SessionLocal() as session:
        from uuid import uuid4

        session.add(
            AuditEventRow(
                id=uuid4(),
                created_at=old,
                event_type=ACTIVITY_LOG_EVENT,
                actor="scanner",
                payload={"message": "stale", "level": "info"},
                payload_text='{"message":"stale"}',
            )
        )
        session.commit()

    BOARD.log("scanner", "fresh")
    assert len(audit_engine.list_events(limit=100)) == 2

    deleted = prune_audit_events(audit_engine, retention_days=30)
    assert deleted == 1
    remaining = audit_engine.list_events(limit=100)
    assert len(remaining) == 1
    assert remaining[0]["message"] == "fresh"
