"""Durable activity log API — reads audit_events, not the in-memory ring buffer."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from core.audit import ACTIVITY_LOG_EVENT, DbAudit, create_audit
from core.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["logs"])


def _parse_before(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@router.get("/logs/events")
async def list_log_events(
    limit: int = Query(default=500, ge=1, le=2000),
    before: str | None = Query(default=None, description="ISO timestamp — page older events"),
    agent: str | None = Query(default=None),
    event_type: str = Query(default=ACTIVITY_LOG_EVENT),
) -> dict:
    audit = create_audit()
    if not isinstance(audit, DbAudit):
        return {"events": [], "retention_days": get_settings().audit_retention_days}

    events = audit.list_events(
        limit=limit,
        before=_parse_before(before),
        event_type=event_type or None,
        actor=agent,
    )
    return {
        "events": events,
        "retention_days": get_settings().audit_retention_days,
        "has_more": len(events) >= limit,
    }
