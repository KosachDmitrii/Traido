"""WAIT trigger → fresh re-evaluation. Never places a broker order."""

from __future__ import annotations

from datetime import UTC, datetime

from core.enums import EntryDecision, EntryWatchStatus
from core.schemas import EntryWatch, Quote
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.entry_watches import (
    ENTRY_WATCHES,
    PRICE_ENTERS_ZONE,
    SUPPORT_BREAK,
    WAIT_EXPIRED,
    price_in_zone,
)
from trading.target_model import build_target_plan


class WaitRevalidationError(RuntimeError):
    """A triggered wait must not become exposure without a fresh pass."""


def observe_price(watch: EntryWatch, price: float) -> EntryWatch:
    """Mark TRIGGERED when price enters the zone — still not executable."""
    if watch.status is not EntryWatchStatus.WAITING:
        return watch
    if datetime.now(UTC) > watch.valid_until:
        return ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.EXPIRED, reason=WAIT_EXPIRED) or watch
    if price_in_zone(price, watch):
        return (
            ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.TRIGGERED, reason=PRICE_ENTERS_ZONE)
            or watch
        )
    return watch


def revalidate_triggered_watch(
    watch: EntryWatch,
    *,
    exec_snap,
    quote: Quote | None,
    market=None,
) -> EntryDecision:
    """Fresh EntryTiming after TRIGGERED. Never returns a broker action.

    Stale/missing quote → NO_TRADE. Deteriorated timing → WAIT or NO_TRADE.
    Only BUY_NOW means the caller may run the existing DecisionPipeline + Risk
    and publish a new opportunity — never convert the watch into an order.
    """
    if watch.status is EntryWatchStatus.EXPIRED:
        raise WaitRevalidationError("WAIT_EXPIRED")
    if watch.status is not EntryWatchStatus.TRIGGERED:
        raise WaitRevalidationError(f"WAIT_NOT_TRIGGERED:{watch.status.value}")
    if quote is None or quote.ask is None:
        return EntryDecision.NO_TRADE


    facts = evaluate_timing(
        exec_snap,
        signal_price=float(watch.signal_price),
        planned_entry=float(watch.planned_entry),
        planned_stop=float(watch.planned_stop),
        planned_target=float(watch.planned_target),
        market=market,
    )
    # Prefer live ask as current price when available.
    facts = facts.model_copy(update={"current_price": float(quote.ask)})
    if facts.nearest_support is not None and float(quote.ask) < facts.nearest_support:
        ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.INVALIDATED, reason=SUPPORT_BREAK)
        return EntryDecision.NO_TRADE

    from trading.historical_mfe import lookup_mfe

    hist_mfe, hist_n = lookup_mfe(
        strategy_version=watch.strategy_version, horizon_min=60
    )
    target = build_target_plan(
        entry=watch.planned_entry,
        stop=watch.planned_stop,
        facts=facts,
        historical_mfe_pct=hist_mfe,
        historical_sample_size=hist_n,
    )
    bundle = decide_entry(
        watch.thesis,
        facts,
        market=market,
        target=target,
        stop_price=float(watch.planned_stop),
    )
    if bundle.entry_decision is EntryDecision.BUY_NOW:
        # Caller must still run DecisionPipeline + Risk. We only mark converted
        # after a successful opportunity publish.
        return EntryDecision.BUY_NOW
    if bundle.entry_decision is EntryDecision.NO_TRADE:
        ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.INVALIDATED, reason="REVALIDATED_NO_TRADE")
        return EntryDecision.NO_TRADE
    # Stay waiting with refreshed reasons.
    ENTRY_WATCHES.update(
        watch.model_copy(
            update={
                "status": EntryWatchStatus.WAITING,
                "reasons": [*watch.reasons, "REVALIDATED_STILL_WAIT", *bundle.chase_reasons[:3]],
            }
        )
    )
    return EntryDecision.WAIT_FOR_ENTRY
