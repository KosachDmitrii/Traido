"""Background EntryWatch loop — WAIT trigger → fresh re-check → maybe publish.

Never places a broker order. BUY_NOW after revalidation only publishes a desk
opportunity through the existing Risk path; the human still confirms.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from core.activity import BOARD
from core.audit import create_audit
from core.config import Settings, get_settings
from core.desk_bus import DESK_BUS
from core.enums import EntryDecision, EntryWatchStatus, RiskVerdict, Timeframe
from core.schemas import EntryWatch, PipelineResult, Quote, TradeAdmissionResult
from market_data.factory import create_market_data_port
from quant.engine import compute_features
from risk.context_builder import build_risk_context
from risk.limits import default_risk_limits
from risk.risk_engine import RiskEngine
from trading.entry_watch_eval import (
    build_candidate_from_revalidation,
    observe_price,
    revalidate_triggered_watch,
)
from trading.entry_watches import ENTRY_WATCHES
from trading.historical_mfe import ensure_seeded_from_aftermath, sync_from_paper_journal
from trading.pipeline import publish_opportunity
from trading.scan_context import open_scan_context
from trading.wait_plan import stale_invalidate_reason
from trading.watch_enrichment import refresh_watch_desk_cache

logger = logging.getLogger(__name__)

WATCH_INTERVAL_SEC = 30.0
_JOURNAL_SYNC_EVERY = 20  # passes ≈ 10 minutes at 30s

_loop_task: asyncio.Task[None] | None = None
_pass_count = 0


async def _mark_price(md: Any, symbol: str, quote: Quote | None) -> float | None:
    """Last trade when available — matches Alpaca dashboard; else quote mid."""
    if hasattr(md, "get_last_price"):
        try:
            last = float(await md.get_last_price(symbol))
            if last > 0:
                return last
        except Exception:
            logger.debug("watch: last trade unavailable for %s", symbol, exc_info=True)
    if quote is not None:
        bid = float(quote.bid or 0)
        ask = float(quote.ask or 0)
        if bid > 0 and ask >= bid:
            return (bid + ask) / 2.0
        return float(quote.ask or quote.bid or 0) or None
    return None


async def run_watch_pass() -> dict[str, int]:
    """One pass over actionable watches. Safe to call from tests."""
    from trading.entry_watch_transitions import recover_stale_leases

    recover_stale_leases()
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
                quote = await md.get_quote(watch.symbol)

            price: float | None = await _mark_price(md, watch.symbol, quote)
            if price is None:
                end = datetime.now(UTC)
                bars = await md.get_bars(watch.symbol, Timeframe.M5, end - timedelta(hours=2), end)
                if bars:
                    price = float(bars[-1].close)
            if price is None:
                continue

            from trading.shadow_outcomes import SHADOW_OUTCOMES

            SHADOW_OUTCOMES.update_price(watch.symbol, price)

            current = watch
            if current.status is EntryWatchStatus.WAITING:
                stale = stale_invalidate_reason(current, price)
                if stale is not None:
                    marked = ENTRY_WATCHES.mark(
                        current.id, EntryWatchStatus.INVALIDATED, reason=stale
                    )
                    if marked is not None:
                        from trading.shadow_outcomes import maybe_begin_shadow_for_terminal_watch

                        maybe_begin_shadow_for_terminal_watch(marked)
                    DESK_BUS.bump_desk(kind="entry_watch_invalidated", symbol=current.symbol)
                    stats["invalidated"] += 1
                    continue
                current = observe_price(current, price)
                if current.status is EntryWatchStatus.EXPIRED:
                    from trading.shadow_outcomes import maybe_begin_shadow_for_terminal_watch

                    maybe_begin_shadow_for_terminal_watch(current)
                    stats["invalidated"] += 1
                    continue
                if current.status is EntryWatchStatus.WAITING:
                    # Desk «Сейчас» used to freeze at creation; keep last tick.
                    prev = float(current.last_price) if current.last_price is not None else None
                    px = Decimal(str(round(price, 4)))
                    ENTRY_WATCHES.update(
                        current.model_copy(
                            update={
                                "last_price": px,
                                "last_observed_at": datetime.now(UTC),
                            }
                        )
                    )
                    refreshed = await refresh_watch_desk_cache(
                        ENTRY_WATCHES.get(current.id) or current,
                        price=price,
                        quote=quote,
                        md=md,
                        prev_price=prev,
                    )
                    ENTRY_WATCHES.update(refreshed)
                    from trading.watch_telemetry import record_watch_telemetry

                    record_watch_telemetry(
                        refreshed,
                        price=price,
                        enrichment=refreshed.desk_enrichment,
                    )
                    if prev is None or abs(price - prev) / max(abs(prev), 1e-9) >= 0.0005:
                        DESK_BUS.bump_desk(kind="entry_watch_price", symbol=current.symbol)
                    stats["still_waiting"] += 1
                    continue

            if current.status is EntryWatchStatus.ADMITTED:
                stats["triggered"] += 1
                await _convert_admitted_watch(
                    current, md, settings, audit, stats, price=price, quote=quote
                )
                continue

            if current.status is EntryWatchStatus.CONVERTING:
                stats["still_waiting"] += 1
                continue

            if current.status is not EntryWatchStatus.TRIGGERED:
                continue

            stats["triggered"] += 1

            end = datetime.now(UTC)
            bars_h1 = await md.get_bars(watch.symbol, Timeframe.H1, end - timedelta(days=60), end)
            if len(bars_h1) < 30:
                await audit.append(
                    "EntryWatchRevalidateSkipped",
                    "entry_timing",
                    {"watch_id": str(watch.id), "reason": "INSUFFICIENT_BARS"},
                )
                continue

            snap = compute_features(watch.symbol, Timeframe.H1, bars_h1)
            q = quote
            if q is None or q.bid is None or q.ask is None:
                # No real top-of-book → cannot admit to buy; stay in wait/trigger cycle.
                await audit.append(
                    "EntryWatchRevalidateSkipped",
                    "entry_timing",
                    {"watch_id": str(watch.id), "reason": "NO_TOP_OF_BOOK"},
                )
                stats["still_waiting"] += 1
                continue

            cached = await refresh_watch_desk_cache(
                ENTRY_WATCHES.get(current.id) or current,
                price=price,
                quote=q,
                md=md,
                prev_price=float(current.last_price) if current.last_price is not None else None,
            )
            ENTRY_WATCHES.update(cached)
            current = ENTRY_WATCHES.get(current.id) or current

            decision, admission = revalidate_triggered_watch(
                current,
                exec_snap=snap,
                quote=q,
                bars=bars_h1,
            )
            if decision is EntryDecision.NO_TRADE:
                stats["invalidated"] += 1
                continue
            if decision is EntryDecision.WAIT_FOR_ENTRY:
                stats["still_waiting"] += 1
                continue

            await _convert_admitted_watch(
                ENTRY_WATCHES.get(current.id) or current,
                md,
                settings,
                audit,
                stats,
                price=price,
                quote=q,
                admission=admission,
            )

        except Exception:
            logger.warning("entry watch pass failed for %s", watch.symbol, exc_info=True)

    from trading.shadow_outcomes import SHADOW_OUTCOMES

    SHADOW_OUTCOMES.finalize_expired()
    return stats


async def _convert_admitted_watch(
    watch: EntryWatch,
    md: Any,
    settings: Settings,
    audit: Any,
    stats: dict[str, int],
    *,
    price: float,
    quote: Quote | None,
    admission: TradeAdmissionResult | None = None,
) -> None:
    """ADMITTED → CONVERTING → publish opportunity → CONVERTED."""
    from core.enums import EntryWatchStatus

    current = ENTRY_WATCHES.get(watch.id) or watch
    if current.status is not EntryWatchStatus.ADMITTED:
        return

    admission_key = f"watch:{current.id}:{current.trigger_version}"
    if not ENTRY_WATCHES.claim_admission(admission_key):
        stats["still_waiting"] += 1
        return

    converting = ENTRY_WATCHES.mark(
        current.id, EntryWatchStatus.CONVERTING, reason="CONVERSION_CLAIM"
    )
    if converting is None:
        stats["still_waiting"] += 1
        return
    current = converting

    base = current.candidate
    if base is None:
        ENTRY_WATCHES.mark(current.id, EntryWatchStatus.INVALIDATED, reason="MISSING_CANDIDATE")
        stats["invalidated"] += 1
        return

    q = quote
    if q is None or q.bid is None or q.ask is None:
        ENTRY_WATCHES.mark(current.id, EntryWatchStatus.ADMITTED, reason="NO_TOP_OF_BOOK")
        stats["still_waiting"] += 1
        return

    if admission is None:
        if not current.last_admission_record_id:
            ENTRY_WATCHES.mark(
                current.id, EntryWatchStatus.INVALIDATED, reason="MISSING_ADMISSION_RECORD"
            )
            stats["invalidated"] += 1
            return
        try:
            admission = _admission_from_record(current)
        except ValueError:
            ENTRY_WATCHES.mark(
                current.id, EntryWatchStatus.ADMITTED, reason="MISSING_ADMISSION_RECORD"
            )
            stats["still_waiting"] += 1
            return

    forced = build_candidate_from_revalidation(current, base=base, admission=admission, quote=q)

    if forced is None:
        ENTRY_WATCHES.mark(
            current.id, EntryWatchStatus.INVALIDATED, reason="REVALIDATION_GEOMETRY_MISMATCH"
        )
        stats["invalidated"] += 1
        return

    from trading.market_gate import evaluate_market_gate_for_candidate

    gate = evaluate_market_gate_for_candidate(forced)
    if not gate.tradable_long:
        ENTRY_WATCHES.mark(
            current.id,
            EntryWatchStatus.INVALIDATED,
            reason=f"REGIME_BLOCKED:{','.join(gate.reason_codes[:2])}",
        )
        stats["invalidated"] += 1
        return

    async with open_scan_context(settings) as ctx:
        built = await build_risk_context(
            forced.symbol,
            broker=ctx.broker,
            market_data=ctx.market_data,
            finnhub_api_key=settings.finnhub_api_key,
            regime_tradable=gate.tradable_long,
        )
        risk = RiskEngine(default_risk_limits()).evaluate(
            forced, await ctx.portfolio(), context=built.context
        )
        if risk.verdict != RiskVerdict.PASS:
            ENTRY_WATCHES.mark(
                current.id,
                EntryWatchStatus.INVALIDATED,
                reason=f"RISK_REJECT:{','.join(risk.reasons[:2])}",
            )
            stats["invalidated"] += 1
            await audit.append(
                "EntryWatchRiskRejected",
                "entry_timing",
                {"watch_id": str(current.id), "reasons": risk.reasons},
            )
            return

        adm = admission or _admission_from_record(current)
        result = PipelineResult(
            pipeline_run_id=forced.pipeline_run_id or uuid4(),
            symbol=forced.symbol,
            status="completed",
            candidate=forced,
        )
        published = await publish_opportunity(
            result, risk, settings=settings, admission=adm, quote=q
        )
        if published.opportunity is not None:
            ENTRY_WATCHES.mark(
                current.id,
                EntryWatchStatus.CONVERTED,
                reason="PUBLISHED_BUY_NOW",
                converted_opportunity_id=published.opportunity.id,
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
                    "watch_id": str(current.id),
                    "opportunity_id": str(published.opportunity.id),
                    "symbol": forced.symbol,
                },
            )
        else:
            ENTRY_WATCHES.mark(current.id, EntryWatchStatus.ADMITTED, reason="PUBLISH_DEFERRED")
            stats["still_waiting"] += 1


def _admission_from_record(watch: EntryWatch) -> TradeAdmissionResult:
    """Rebuild minimal admission from last persisted record for restart recovery."""
    from trading.admission_records import ADMISSION_RECORDS

    if not watch.last_admission_record_id:
        raise ValueError("no admission record")
    rec = ADMISSION_RECORDS.get(watch.last_admission_record_id)
    if rec is None:
        raise ValueError("admission record missing")
    return TradeAdmissionResult(
        decision=rec.decision,
        admitted=rec.admitted,
        setup_type=rec.setup_type,
        setup_quality=rec.setup_quality,
        entry_quality=rec.entry_quality,
        effective_rr=rec.effective_rr,
        chase_score=rec.chase_score,
        structure_valid=rec.structure_valid,
        stop_valid=rec.stop_valid,
        target_valid=rec.target_valid,
        data_status=rec.data_status,
        vetoes=list(rec.vetoes),
        warnings=list(rec.warnings),
        reason_codes=list(rec.reason_codes),
        admission_version=rec.admission_version,
        snapshot=watch.admission_snapshot,
    )


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
