"""ORB observation and durable end-of-session exits, independent of browser presence."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from strategy.orb.runtime import observe

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None
_exit_lock = asyncio.Lock()


async def exit_due_positions(*, now: datetime | None = None) -> int:
    from api.deps import build_execution_service
    from trading.ledger import LEDGER

    if _exit_lock.locked():
        return 0
    async with _exit_lock:
        count = 0
        now = now or datetime.now(UTC)
        for row in LEDGER.get_open():
            payload = row.payload or {}
            if payload.get("exit_policy") != "session_close" or not payload.get("exit_at"):
                continue
            deadline = datetime.fromisoformat(payload["exit_at"])
            if deadline.tzinfo is None or now < deadline:
                continue
            try:
                # Uses the existing exit claim, broker identity, durable intent and fill reconciliation.
                await build_execution_service().close_position(row.symbol, reason="ORB_SESSION_END")
                count += 1
            except Exception:
                logger.exception("ORB scheduled exit unresolved for %s; will retry", row.symbol)
        return count


async def _run() -> None:
    while True:
        try:
            await observe()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ORB observation pass failed")
        await asyncio.sleep(5)


def start_entry_watch_loop() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run())


def stop_entry_watch_loop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
