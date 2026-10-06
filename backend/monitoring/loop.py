"""Isolated observer: failures never stop or change trading."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from monitoring.service import record_sample, refresh_report

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None
_status: dict[str, Any] = {"last_success_at": None, "last_error": None}


async def _in_thread(fn: Callable[[], Any]) -> None:
    # Drain a tick before shutdown: cancelling to_thread alone leaves its DB
    # writes running after the observer claims to have stopped.
    tick = asyncio.create_task(asyncio.to_thread(fn))
    try:
        await asyncio.shield(tick)
    except asyncio.CancelledError:
        try:
            await tick
        except Exception as exc:  # noqa: BLE001 — drain failure must not swallow cancellation
            logger.warning("observation shutdown failed: %s", type(exc).__name__)
        raise


async def _run(interval: float) -> None:
    report_bucket = None
    while True:
        try:
            await _in_thread(record_sample)
            bucket = int(datetime.now(UTC).timestamp()) // 300
            if bucket != report_bucket:
                await _in_thread(refresh_report)
                report_bucket = bucket
            _status.update(last_success_at=datetime.now(UTC).isoformat(), last_error=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — observer must survive a failed tick
            # Exception text can contain DB connection credentials; retain only the type.
            _status["last_error"] = type(exc).__name__
            logger.warning("observation failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


def status() -> dict[str, Any]:
    return {
        **_status,
        "running": _task is not None and not _task.done(),
        "interval_seconds": 60,
        "report_interval_seconds": 300,
    }


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run(60), name="forward-observation")


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
