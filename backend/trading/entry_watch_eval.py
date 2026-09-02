"""WAIT trigger → fresh re-evaluation. Never places a broker order."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from core.enums import AdmissionDecision, EntryDecision, EntryWatchStatus, SetupType
from core.schemas import (
    Bar,
    EntryWatch,
    FeatureSnapshot,
    MarketAssessment,
    Quote,
    StopPlan,
    TradeAdmissionResult,
    TradeCandidate,
    WatchRevalidationResult,
)
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.entry_watches import (
    ENTRY_WATCHES,
    PRICE_ENTERS_ZONE,
    SUPPORT_BREAK,
    WAIT_EXPIRED,
    price_in_zone,
)
from trading.geometry_hash import compute_geometry_hash, geometry_hash_from_watch
from trading.target_model import build_target_plan
from trading.trade_admission import evaluate_trade_admission
from trading.wait_conditions import unmet_wait_conditions
from trading.watch_desk import zone_arrival_required_for
from trading.zone_arrival import evaluate_zone_arrival


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
    exec_snap: FeatureSnapshot,
    quote: Quote | None,
    market: MarketAssessment | None = None,
    bars: list[Bar] | None = None,
) -> tuple[EntryDecision, TradeAdmissionResult | None]:
    """Fresh EntryTiming after TRIGGERED. TradeAdmission is the BUY authority."""
    result = revalidate_triggered_watch_full(
        watch, exec_snap=exec_snap, quote=quote, market=market, bars=bars
    )
    if result is None:
        return EntryDecision.NO_TRADE, None
    return result.entry_decision, result.admission


def revalidate_triggered_watch_full(
    watch: EntryWatch,
    *,
    exec_snap: FeatureSnapshot,
    quote: Quote | None,
    market: MarketAssessment | None = None,
    bars: list[Bar] | None = None,
) -> WatchRevalidationResult | None:
    """Full revalidation with immutable geometry result."""
    if watch.status is EntryWatchStatus.EXPIRED:
        raise WaitRevalidationError("WAIT_EXPIRED")
    if watch.status is not EntryWatchStatus.TRIGGERED:
        raise WaitRevalidationError(f"WAIT_NOT_TRIGGERED:{watch.status.value}")
    if quote is None or quote.ask is None:
        return None

    marked = ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.REVALIDATING)
    if marked is None:
        return None
    watch = marked

    facts = evaluate_timing(
        exec_snap,
        signal_price=float(watch.signal_price),
        planned_entry=float(watch.planned_entry),
        planned_stop=float(watch.planned_stop),
        planned_target=float(watch.planned_target),
        market=market,
    )
    facts = facts.model_copy(update={"current_price": float(quote.ask)})
    if facts.nearest_support is not None and float(quote.ask) < facts.nearest_support:
        ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.INVALIDATED, reason=SUPPORT_BREAK)
        return None

    from trading.historical_mfe import lookup_mfe

    hist_mfe, hist_n = lookup_mfe(strategy_version=watch.strategy_version, horizon_min=60)
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
        stop_price=float(watch.planned_stop),
        target=target,
    )
    pending = unmet_wait_conditions(watch, facts, quote=quote)
    if pending:
        ENTRY_WATCHES.update(
            watch.model_copy(
                update={
                    "status": EntryWatchStatus.WAITING,
                    "reasons": [
                        *watch.reasons,
                        "TRIGGERED_CONDITIONS_PENDING",
                        *pending[:4],
                    ],
                }
            )
        )
        return None

    setup_type = watch.setup_type or SetupType.PULLBACK_CONTINUATION
    zone_arrival_quality: int | None = None
    zone_arrival_type: str | None = None
    arrival_facts = None

    if zone_arrival_required_for(setup_type) and bars:
        atr = facts.atr
        if watch.admission_snapshot and watch.admission_snapshot.atr_at_creation:
            atr = atr or watch.admission_snapshot.atr_at_creation
        arrival_facts = evaluate_zone_arrival(watch, bars, atr=atr, current_price=float(quote.ask))
        zone_arrival_quality = round(arrival_facts.score)
        zone_arrival_type = arrival_facts.arrival_type.value

    candidate = watch.candidate
    from trading.data_integrity import last_bar_timestamp

    admission = evaluate_trade_admission(
        bundle=bundle,
        candidate=candidate,
        setup_type=setup_type,
        quote=quote,
        bars_count=len(bars) if bars else 0,
        last_bar_ts=last_bar_timestamp(bars) if bars else None,
        require_bars=True,
        entry=watch.planned_entry,
        stop=watch.planned_stop,
        target=target.price if target else watch.planned_target,
        target_plan=target,
        zone_arrival=arrival_facts,
    )

    from trading.admission_records import persist_admission

    record = persist_admission(
        symbol=watch.symbol,
        admission=admission,
        watch_id=watch.id,
        pipeline_run_id=watch.pipeline_run_id,
        trigger_version=watch.trigger_version,
        zone_arrival_quality=zone_arrival_quality,
        zone_arrival_type=zone_arrival_type,
        context={"source": "watch_revalidate", "phase": "watch_revalidation"},
        geometry_hash=geometry_hash_from_watch(watch),
    )

    if admission.decision is AdmissionDecision.DATA_BLOCKED:
        ENTRY_WATCHES.update(
            watch.model_copy(
                update={
                    "status": EntryWatchStatus.WAITING,
                    "reasons": [*watch.reasons, *admission.reason_codes[:6]],
                }
            )
        )
        return WatchRevalidationResult(
            entry_decision=EntryDecision.WAIT_FOR_ENTRY,
            admission=admission,
            quote=quote,
            evaluated_at=datetime.now(UTC),
            geometry_hash=geometry_hash_from_watch(watch),
        )

    gh = geometry_hash_from_watch(watch)
    if admission.snapshot and admission.admitted:
        snap_gh = compute_geometry_hash(
            entry=float(watch.planned_entry),
            stop=float(watch.planned_stop),
            target=float(target.price if target else watch.planned_target),
            exec_timeframe=watch.exec_timeframe,
            strategy_version=watch.strategy_version,
        )
        if snap_gh != gh:
            ENTRY_WATCHES.mark(
                watch.id, EntryWatchStatus.INVALIDATED, reason="GEOMETRY_VERSION_MISMATCH"
            )
            return None

    stop_plan = StopPlan(
        price=watch.planned_stop,
        model="structure",
        atr_distance=facts.stop_distance_atr,
        reason_codes=admission.reason_codes[:4],
    )

    if admission.decision is AdmissionDecision.BUY_ALLOWED:
        ENTRY_WATCHES.mark(
            watch.id,
            EntryWatchStatus.ADMITTED,
            reason="ADMISSION_PASSED",
            last_admission_record_id=record.id,
        )
        built = build_candidate_from_revalidation(
            watch, base=candidate or _minimal_candidate(watch), admission=admission, quote=quote
        )
        return WatchRevalidationResult(
            entry_decision=EntryDecision.BUY_NOW,
            admission=admission,
            candidate=built,
            target_plan=target,
            stop_plan=stop_plan,
            quote=quote,
            snapshot=admission.snapshot,
            evaluated_at=datetime.now(UTC),
            geometry_hash=gh,
        )

    if admission.decision is AdmissionDecision.NO_TRADE:
        ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.INVALIDATED, reason="REVALIDATED_NO_TRADE")
        return WatchRevalidationResult(
            entry_decision=EntryDecision.NO_TRADE,
            admission=admission,
            quote=quote,
            evaluated_at=datetime.now(UTC),
            geometry_hash=gh,
        )

    ENTRY_WATCHES.update(
        watch.model_copy(
            update={
                "status": EntryWatchStatus.TRIGGERED,
                "reasons": [*watch.reasons, *admission.reason_codes[:6]],
            }
        )
    )
    return WatchRevalidationResult(
        entry_decision=EntryDecision.WAIT_FOR_ENTRY,
        admission=admission,
        quote=quote,
        evaluated_at=datetime.now(UTC),
        geometry_hash=gh,
    )


def _minimal_candidate(watch: EntryWatch) -> TradeCandidate:
    from core.enums import TradeAction

    return TradeCandidate(
        symbol=watch.symbol,
        action=TradeAction.BUY,
        confidence=0.5,
        entry=watch.planned_entry,
        stop=watch.planned_stop,
        target=watch.planned_target,
        risk_reward=float(watch.planned_risk_reward or 2.0),
        reasons=["watch_revalidation"],
        strategy_version=watch.strategy_version,
        pipeline_run_id=watch.pipeline_run_id or uuid4(),
    )


def build_candidate_from_revalidation(
    watch: EntryWatch,
    *,
    base: TradeCandidate,
    admission: TradeAdmissionResult,
    quote: Quote,
) -> TradeCandidate | None:
    """Immutable candidate from fresh revalidation — never reuse stale geometry."""
    if admission.decision is not AdmissionDecision.BUY_ALLOWED or not admission.admitted:
        return None
    if admission.snapshot is None:
        return None
    ask = float(quote.ask or watch.planned_entry)
    entry = Decimal(str(round(ask, 4)))
    stop = watch.planned_stop
    target_px = watch.planned_target
    if not (stop < entry < target_px):
        return None
    rr = float((target_px - entry) / (entry - stop))
    snap = admission.snapshot
    gh = geometry_hash_from_watch(watch)
    return base.model_copy(
        update={
            "entry_decision": EntryDecision.BUY_NOW,
            "entry": entry,
            "stop": stop,
            "target": target_px,
            "risk_reward": round(rr, 2),
            "entry_quality": admission.entry_quality,
            "setup_quality": admission.setup_quality,
            "admission_version": admission.admission_version,
            "admission_snapshot": snap.model_dump(mode="json"),
            "effective_rr_at_creation": admission.effective_rr,
            "pipeline_run_id": uuid4(),
            "market_label": base.market_label,
            "reasons": [
                *base.reasons[:8],
                "WAIT_TRIGGERED_REEVAL_BUY_NOW",
                f"watch_id={watch.id}",
                f"geometry_hash={gh}",
            ],
        }
    )
