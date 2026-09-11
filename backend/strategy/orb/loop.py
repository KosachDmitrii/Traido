"""ORB observation and durable end-of-session exits, independent of browser presence."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from strategy.orb.runtime import observe
from trading.exit_policy import manual_target_exits

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
        fixed_now = now
        now = now or datetime.now(UTC)
        for row in LEDGER.get_open():
            payload = row.payload or {}
            if payload.get("exit_policy") != "session_close" or not payload.get("exit_at"):
                continue
            try:
                deadline = datetime.fromisoformat(payload["exit_at"])
                if deadline.tzinfo is None:
                    continue
                execution = None
                manual_target = manual_target_exits()
                reason = "ORB_SESSION_END" if not manual_target and now >= deadline else None
                raw_plan = payload.get("orb_plan") or {}
                if reason is None and raw_plan.get("version") == "orb@2.0.0" and row.qty > 0:
                    from strategy.orb import PARAMETERS, OrbPlan
                    from strategy.orb.position_policy import observed_target

                    plan = OrbPlan.model_validate(raw_plan)
                    target = observed_target(payload)
                    if target is None:
                        continue
                    execution = build_execution_service()
                    quote = (
                        await execution.quotes.get_quote(row.symbol) if execution.quotes else None
                    )
                    now = fixed_now or datetime.now(UTC)
                    if (
                        quote is None
                        or quote.ts.tzinfo is None
                        or quote.symbol != row.symbol
                        or not -1 <= (now - quote.ts).total_seconds() <= 5
                        or not quote.bid.is_finite()
                        or not quote.ask.is_finite()
                        or quote.bid <= 0
                        or quote.ask < quote.bid
                        or (quote.feed is not None and plan.source != f"alpaca:{quote.feed}")
                    ):
                        continue
                    if quote.bid >= target:
                        reason = "ORB_TARGET_REACHED"
                    elif (
                        not manual_target
                        and row.opened_at.tzinfo is not None
                        and now
                        >= row.opened_at + timedelta(minutes=PARAMETERS["time_exit_minutes"])
                        and quote.bid <= Decimal(str(row.avg_entry))
                    ):
                        reason = "ORB_TIME_NO_PROGRESS"
                if reason is None:
                    continue
                if execution is not None:
                    await execution.audit.append(
                        "OrbExitTriggered",
                        "orb",
                        {
                            "symbol": row.symbol,
                            "version": raw_plan.get("version"),
                            "reason": reason,
                            "target": str(target),
                            "avg_entry": str(row.avg_entry),
                            "quote": quote.model_dump(mode="json"),
                            "evaluated_at": now.isoformat(),
                        },
                    )
                # One existing exit owner cancels/verifies protection and rereads
                # signed broker holdings. No competing take-profit SELL is placed.
                await (execution or build_execution_service()).close_position(
                    row.symbol, reason=reason
                )
                count += 1
            except Exception:
                logger.exception("ORB scheduled exit unresolved for %s; will retry", row.symbol)
        return count


async def _run() -> None:
    next_access_check = 0.0
    while True:
        try:
            if time.monotonic() >= next_access_check:
                from core.config import get_settings
                from market_data.factory import create_market_data_port
                from strategy.orb.data_access import check_access

                await check_access(create_market_data_port(get_settings()))
                next_access_check = time.monotonic() + 60
            await observe()
            from core.audit import create_audit
            from trading.auto_trigger_policy import enqueue_auto_approve_open_buys

            enqueue_auto_approve_open_buys(audit=create_audit())
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
