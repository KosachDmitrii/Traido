"""Prune durable audit rows older than the configured retention window."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from core.audit import DbAudit
from core.config import get_settings

logger = logging.getLogger(__name__)

_PRUNE_INTERVAL_SEC = 24 * 3600


def prune_audit_events(audit: DbAudit, *, retention_days: int | None = None) -> int:
    days = retention_days if retention_days is not None else get_settings().audit_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    deleted = audit.prune_before(cutoff)
    if deleted:
        logger.info(
            "audit retention: pruned %s rows older than %s days",
            deleted,
            days,
        )
    return deleted


async def _retention_loop(audit: DbAudit, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(prune_audit_events, audit)
        except Exception:  # noqa: BLE001 — retention must not take down the API
            logger.exception("audit retention pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=_PRUNE_INTERVAL_SEC)
        except TimeoutError:
            continue


def start_log_retention(audit: DbAudit) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(_retention_loop(audit, stop), name="log-retention")
    return stop, task


async def stop_log_retention(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
