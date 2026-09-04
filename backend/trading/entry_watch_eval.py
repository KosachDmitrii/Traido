"""WAIT trigger → fresh re-evaluation. Never places a broker order."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from core.enums import (
    AdmissionDecision,
    EntryDecision,
    EntryWatchStatus,
    SetupType,
    TargetReachabilityClass,
)
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
from trading.entry_policy import get_entry_thresholds
from trading.entry_watches import (
    ENTRY_WATCHES,
    PRICE_ENTERS_ZONE,
    SUPPORT_BREAK,
    WAIT_EXPIRED,
    ZONE_RECLAIM,
    price_in_zone,
)
from trading.zone_geometry import (
    record_zone_touch,
    reset_zone_touch,
    structure_lost_below_zone,
    zone_reclaim_met,
    zone_touch_exhausted,
)
from trading.geometry_hash import compute_geometry_hash, geometry_hash_from_watch
from trading.target_model import build_target_plan
from trading.trade_admission import evaluate_trade_admission
from trading.wait_conditions import TRANSIENT_TRIGGER_CONDITIONS, unmet_wait_conditions
from trading.watch_desk import zone_arrival_required_for
from trading.zone_arrival import evaluate_zone_arrival


class WaitRevalidationError(RuntimeError):
    """A triggered wait must not become exposure without a fresh pass."""


def observe_price(watch: EntryWatch, price: float, *, atr: float | None = None) -> EntryWatch:
    """Mark TRIGGERED after zone touch + reclaim — still not executable until revalidation."""
    if watch.status is not EntryWatchStatus.WAITING:
        return watch
    if datetime.now(UTC) > watch.valid_until:
        return ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.EXPIRED, reason=WAIT_EXPIRED) or watch

    th = get_entry_thresholds()
    if structure_lost_below_zone(watch, price, atr, th):
        reset_zone_touch(watch.id)
        return (
            ENTRY_WATCHES.mark(watch.id, EntryWatchStatus.INVALIDATED, reason=SUPPORT_BREAK)
            or watch
        )

    prev_px = float(watch.last_price) if watch.last_price is not None else None
    was_in_zone = prev_px is not None and price_in_zone(prev_px, watch)

    if price_in_zone(price, watch):
        if not was_in_zone:
            touches = record_zone_touch(watch.id)
        else:
            from trading.zone_geometry import zone_touch_count

            touches = zone_touch_count(watch.id)
        if zone_touch_exhausted(watch.id, th):
            reset_zone_touch(watch.id)
            return (
                ENTRY_WATCHES.mark(
                    watch.id,
                    EntryWatchStatus.INVALIDATED,
                    reason="PULLBACK_TOUCH_EXHAUSTED",
                )
                or watch
            )
        if zone_reclaim_met(watch, price, th):
            return (
                ENTRY_WATCHES.mark(
                    watch.id,
                    EntryWatchStatus.TRIGGERED,
                    reason=ZONE_RECLAIM if th.zone_require_reclaim else PRICE_ENTERS_ZONE,
                )
                or watch
            )
        return watch
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

    try:
        result = _revalidate_after_claim(
            watch,
            exec_snap=exec_snap,
            quote=quote,
            market=market,
            bars=bars,
        )
    except Exception:
        # Never leave the desk card on «Повторная проверка» after a crash.
        _release_revalidating(watch.id, reason="REVALIDATE_ABORTED")
        raise

    current = ENTRY_WATCHES.get(watch.id)
    if current is not None and current.status is EntryWatchStatus.REVALIDATING:
        # Caller returned without releasing the lease — recover for the desk.
        _release_revalidating(watch.id, reason="REVALIDATE_STILL_CLAIMED")
    return result


def _release_revalidating(watch_id, *, reason: str) -> None:
    if ENTRY_WATCHES.mark(watch_id, EntryWatchStatus.TRIGGERED, reason=reason) is not None:
        return
    current = ENTRY_WATCHES.get(watch_id)
    if current is None or current.status is not EntryWatchStatus.REVALIDATING:
        return
    ENTRY_WATCHES.update(
        current.model_copy(
            update={
                "status": EntryWatchStatus.TRIGGERED,
                "reasons": [*current.reasons, reason],
                "claimed_at": None,
                "claim_token": None,
                "claim_owner_id": None,
                "lease_expires_at": None,
            }
        )
    )


def _revalidate_after_claim(
    watch: EntryWatch,
    *,
    exec_snap: FeatureSnapshot,
    quote: Quote,
    market: MarketAssessment | None,
    bars: list[Bar] | None,
) -> WatchRevalidationResult | None:
    """Body of revalidation while the REVALIDATING lease is held."""
    facts = evaluate_timing(
        exec_snap,
        signal_price=float(watch.signal_price),
        planned_entry=float(watch.planned_entry),
        planned_stop=float(watch.planned_stop),
        planned_target=float(watch.planned_target),
        market=market,
    )
    # Admission / RR still use the ask; zone membership must match the mark that
    # triggered WAIT (last trade), or ask-above-zone flaps reset every pass.
    mark_price = float(watch.last_price) if watch.last_price is not None else float(quote.ask)
    atr_v = facts.atr
    if watch.admission_snapshot and watch.admission_snapshot.atr_at_creation:
        atr_v = atr_v or watch.admission_snapshot.atr_at_creation
    in_cushion = price_in_zone(mark_price, watch, atr=atr_v)
    if in_cushion:
        # Score chase / target at the planned fill inside the printed cushion band.
        facts = evaluate_timing(
            exec_snap,
            signal_price=float(watch.signal_price),
            planned_entry=float(watch.planned_entry),
            planned_stop=float(watch.planned_stop),
            planned_target=float(watch.planned_target),
            market=market,
        )
        eval_price = float(watch.planned_entry)
    else:
        eval_price = float(quote.ask)
    facts_for_wait = facts.model_copy(update={"current_price": mark_price})
    facts = facts.model_copy(update={"current_price": eval_price})
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
    if in_cushion and target.reachability is TargetReachabilityClass.UNREALISTIC:
        target = target.model_copy(
            update={
                "price": watch.planned_target,
                "reachability": TargetReachabilityClass.INSUFFICIENT_DATA,
            }
        )
    bundle = decide_entry(
        watch.thesis,
        facts,
        market=market,
        stop_price=float(watch.planned_stop),
        target=target,
    )
    bundle = bundle.model_copy(
        update={
            "entry_zone_low": watch.entry_zone_low,
            "entry_zone_high": watch.entry_zone_high,
        }
    )
    pending = unmet_wait_conditions(watch, facts_for_wait, quote=quote)
    if pending:
        reason = "TRIGGERED_CONDITIONS_PENDING:" + ",".join(pending[:4])
        stay_triggered = set(pending).issubset(TRANSIENT_TRIGGER_CONDITIONS)
        next_status = EntryWatchStatus.TRIGGERED if stay_triggered else EntryWatchStatus.WAITING
        if ENTRY_WATCHES.mark(watch.id, next_status, reason=reason) is None:
            ENTRY_WATCHES.update(
                watch.model_copy(
                    update={
                        "status": next_status,
                        "reasons": [*watch.reasons, reason],
                        "claimed_at": None,
                        "claim_token": None,
                        "claim_owner_id": None,
                        "lease_expires_at": None,
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

    zone_entry_price = mark_price if in_cushion else float(quote.ask)

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
        zone_entry_price=zone_entry_price,
        cushion_fill=True,
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
        # Transient quote latency — keep TRIGGERED so the next pass retries admission.
        ENTRY_WATCHES.mark(
            watch.id,
            EntryWatchStatus.TRIGGERED,
            reason=",".join(admission.reason_codes[:6]) or "DATA_BLOCKED",
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

    # Soft WAIT from admission — stay TRIGGERED (lease cleared) for the next pass.
    if (
        ENTRY_WATCHES.mark(
            watch.id,
            EntryWatchStatus.TRIGGERED,
            reason=",".join(admission.reason_codes[:6]) or "REVALIDATE_WAIT",
        )
        is None
    ):
        ENTRY_WATCHES.update(
            watch.model_copy(
                update={
                    "status": EntryWatchStatus.TRIGGERED,
                    "reasons": [
                        *watch.reasons,
                        *(admission.reason_codes[:6] or ["REVALIDATE_WAIT"]),
                    ],
                    "claimed_at": None,
                    "claim_token": None,
                    "claim_owner_id": None,
                    "lease_expires_at": None,
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
