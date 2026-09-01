"""Background EntryWatch loop — WAIT trigger → fresh re-check → maybe publish.

Never places a broker order. BUY_NOW after revalidation only publishes a desk
opportunity through the existing Risk path; the human still confirms.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.activity import BOARD
from core.audit import create_audit
from core.config import get_settings
from core.desk_bus import DESK_BUS
from core.enums import EntryDecision, EntryWatchStatus, RiskVerdict, Timeframe
from core.schemas import PipelineResult, Quote
from market_data.factory import create_market_data_port
from quant.engine import compute_features
from risk.context_builder import build_risk_context
from risk.limits import default_risk_limits
from risk.risk_engine import RiskEngine
from trading.entry_watch_eval import observe_price, revalidate_triggered_watch
from trading.entry_watches import ENTRY_WATCHES
from trading.historical_mfe import ensure_seeded_from_aftermath, sync_from_paper_journal
from trading.pipeline import publish_opportunity
from trading.scan_context import open_scan_context

logger = logging.getLogger(__name__)

WATCH_INTERVAL_SEC = 30.0
_JOURNAL_SYNC_EVERY = 20  # passes ≈ 10 minutes at 30s

_loop_task: asyncio.Task[None] | None = None
_pass_count = 0


async def run_watch_pass() -> dict:
    """One pass over actionable watches. Safe to call from tests."""
    watches = ENTRY_WATCHES.list_actionable()
    stats = {
        "checked": 0,
        "triggered": 0,
        "converted": 0,
        "invalidated": 0,
        "still_waiting": 0,
    }
    if not watches:
        return stats

    settings = get_settings()
    md = create_market_data_port(settings)
    audit = create_audit()

    for watch in watches:
        stats["checked"] += 1
        try:
            quote = None
            if hasattr(md, "get_quote"):
                quote = await md.get_quote(watch.symbol)  # type: ignore[attr-defined]

            price: float | None = None
            if quote is not None:
                price = float(quote.ask or quote.bid or 0) or None
            if price is None:
                end = datetime.now(UTC)
                bars = await md.get_bars(
                    watch.symbol, Timeframe.M5, end - timedelta(hours=2), end
                )
                if bars:
                    price = float(bars[-1].close)
            if price is None:
                continue

            current = watch
            if current.status is EntryWatchStatus.WAITING:
                current = observe_price(current, price)
                if current.status is EntryWatchStatus.EXPIRED:
                    stats["invalidated"] += 1
                    continue
                if current.status is EntryWatchStatus.WAITING:
                    stats["still_waiting"] += 1
                    continue

            if current.status is not EntryWatchStatus.TRIGGERED:
                continue

            stats["triggered"] += 1

            end = datetime.now(UTC)
            bars_h1 = await md.get_bars(
                watch.symbol, Timeframe.H1, end - timedelta(days=60), end
            )
            if len(bars_h1) < 30:
                await audit.append(
                    "EntryWatchRevalidateSkipped",
                    "entry_timing",
                    {"watch_id": str(watch.id), "reason": "INSUFFICIENT_BARS"},
                )
                continue

            snap = compute_features(watch.symbol, Timeframe.H1, bars_h1)
            q = quote
            if q is None:
                q = Quote(
                    symbol=watch.symbol,
                    bid=Decimal(str(price)),
                    ask=Decimal(str(price)),
                    ts=datetime.now(UTC),
                    source="watch_loop_bar",
                )

            decision = revalidate_triggered_watch(current, exec_snap=snap, quote=q)
            if decision is EntryDecision.NO_TRADE:
                stats["invalidated"] += 1
                continue
            if decision is EntryDecision.WAIT_FOR_ENTRY:
                stats["still_waiting"] += 1
                continue

            base = watch.candidate
            if base is None:
                ENTRY_WATCHES.mark(
                    watch.id, EntryWatchStatus.INVALIDATED, reason="MISSING_CANDIDATE"
                )
                stats["invalidated"] += 1
                continue

            forced = base.model_copy(
                update={
                    "entry_decision": EntryDecision.BUY_NOW,
                    "entry": watch.planned_entry,
                    "stop": watch.planned_stop,
                    "target": watch.planned_target,
                    "pipeline_run_id": uuid4(),
                    "reasons": [
                        *base.reasons,
                        "WAIT_TRIGGERED_REEVAL_BUY_NOW",
                        f"watch_id={watch.id}",
                    ],
                }
            )

            async with open_scan_context(settings) as ctx:
                built = await build_risk_context(
                    forced.symbol,
                    broker=ctx.broker,
                    market_data=ctx.market_data,
                    finnhub_api_key=settings.finnhub_api_key,
                    regime_tradable=True,
                )
                risk = RiskEngine(default_risk_limits()).evaluate(
                    forced, await ctx.portfolio(), context=built.context
                )
                if risk.verdict != RiskVerdict.PASS:
                    ENTRY_WATCHES.mark(
                        watch.id,
                        EntryWatchStatus.INVALIDATED,
                        reason=f"RISK_REJECT:{','.join(risk.reasons[:2])}",
                    )
                    stats["invalidated"] += 1
                    await audit.append(
                        "EntryWatchRiskRejected",
                        "entry_timing",
                        {"watch_id": str(watch.id), "reasons": risk.reasons},
                    )
                    continue

                result = PipelineResult(
                    pipeline_run_id=forced.pipeline_run_id or uuid4(),
                    symbol=forced.symbol,
                    status="completed",
                    candidate=forced,
                )
                published = await publish_opportunity(result, risk, settings=settings)
                if published.opportunity is not None:
                    ENTRY_WATCHES.mark(
                        watch.id, EntryWatchStatus.CONVERTED, reason="PUBLISHED_BUY_NOW"
                    )
                    stats["converted"] += 1
                    BOARD.log(
                        "strategy",
                        f"WAIT→BUY_NOW published {forced.symbol}",
                        symbol=forced.symbol,
                    )
                    DESK_BUS.bump_desk(kind="entry_watch_converted", symbol=forced.symbol)
                    await audit.append(
                        "EntryWatchConverted",
                        "entry_timing",
                        {
                            "watch_id": str(watch.id),
                            "opportunity_id": str(published.opportunity.id),
                            "symbol": forced.symbol,
                        },
                    )
                else:
                    stats["still_waiting"] += 1

        except Exception:
            logger.warning("entry watch pass failed for %s", watch.symbol, exc_info=True)

    return stats


async def _watch_forever(interval_sec: float) -> None:
    global _pass_count
    logger.info("entry watch loop: started, every %.0fs", interval_sec)
    try:
        n = ensure_seeded_from_aftermath()
        if n:
            logger.info("entry watch loop: seeded %d historical MFE samples", n)
    except Exception:
        logger.warning("entry watch loop: MFE seed failed", exc_info=True)

    while True:
        try:
            stats = await run_watch_pass()
            _pass_count += 1
            if stats["checked"]:
                logger.info("entry watch loop: %s", stats)
            if _pass_count % _JOURNAL_SYNC_EVERY == 0:
                synced = sync_from_paper_journal()
                if synced:
                    logger.info("entry watch loop: synced %d journal MFE rows", synced)
                try:
                    from trading.f3_diagnostics import write_forward_report

                    write_forward_report()
                except Exception:
                    logger.warning("entry watch loop: forward report failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("entry watch loop: pass failed", exc_info=True)
        await asyncio.sleep(interval_sec)


def start_entry_watch_loop(*, interval_sec: float | None = None) -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_watch_forever(interval_sec or WATCH_INTERVAL_SEC))


def stop_entry_watch_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
