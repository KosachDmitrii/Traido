"""Final pretrade validation — re-check entry gates at human approve time.

Never invents BUY_NOW, BULLISH, component scores of 50, REALISTIC reachability,
or nearest_support from the planned stop. Missing facts fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from core.enums import (
    AdmissionDecision,
    DataHealthStatus,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
)
from core.schemas import (
    AdmissionInput,
    AdmissionSnapshot,
    Bar,
    EntryDecisionBundle,
    EntryQualityBreakdown,
    FeatureSnapshot,
    MarketAssessment,
    Quote,
    SetupQualityBreakdown,
    StopPlan,
    TargetPlan,
    TradeAdmissionResult,
    TradeCandidate,
)
from trading.chase_facts import HARD_CHASE_LIMIT, compute_chase_facts
from trading.data_integrity import check_data_integrity
from trading.entry_policy import get_entry_thresholds
from trading.entry_timing import evaluate_timing
from trading.geometry_hash import geometry_hash_from_candidate
from trading.market_gate import MarketGateResult
from trading.trade_admission import (
    ADMISSION_VERSION,
    POLICY_VERSION,
    entry_allowed_for_setup_type,
    evaluate_from_admission_input,
)
from trading.zone_arrival import ZoneArrivalFacts, zone_arrival_required

# Approval drift from last admission/revalidation anchor — not from signal price.
MAX_APPROVAL_DRIFT_PCT = 1.5


class PretradeRejection(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def map_admission_rejection(admission: TradeAdmissionResult) -> str:
    if admission.decision is AdmissionDecision.DATA_BLOCKED:
        return "BUY_REJECTED_STALE_DATA"
    if "TARGET_UNREALISTIC" in admission.vetoes or any(
        "TARGET_UNREALISTIC" in r for r in admission.reason_codes
    ):
        return "NO_TRADE/TARGET_UNREALISTIC"
    if "INSUFFICIENT_EFFECTIVE_RR" in admission.vetoes or any(
        "INSUFFICIENT_EFFECTIVE_RR" in r for r in admission.reason_codes
    ):
        return "BUY_REJECTED_RR_DROPPED"
    if any("REGIME" in v for v in admission.vetoes + admission.reason_codes):
        return "BUY_REJECTED_REGIME"
    if admission.decision is AdmissionDecision.NO_TRADE:
        return "BUY_REJECTED_ADMISSION"
    return "BUY_REJECTED_ADMISSION"


def require_final_admission(admission: TradeAdmissionResult) -> TradeAdmissionResult:
    """Capital path — only BUY_ALLOWED + admitted + healthy data may proceed."""
    if (
        admission.decision is AdmissionDecision.BUY_ALLOWED
        and admission.admitted
        and admission.data_status is DataHealthStatus.HEALTHY
    ):
        return admission
    code = map_admission_rejection(admission)
    detail = ",".join(admission.reason_codes[:8]) or admission.decision.value
    raise PretradeRejection(code, detail)


def _require_target_plan(candidate: TradeCandidate) -> TargetPlan:
    """Propagate original reachability — never force REALISTIC."""
    reachability = candidate.target_reachability
    model = candidate.target_model
    if reachability is None or model is None:
        raise PretradeRejection(
            "BUY_REJECTED_ADMISSION",
            "TARGET_PLAN_REQUIRED",
        )
    return TargetPlan(
        price=candidate.target,
        model=model,
        reachability=reachability,
    )


def _require_breakdown(
    candidate: TradeCandidate,
) -> tuple[EntryQualityBreakdown, SetupQualityBreakdown | None]:
    """Refuse invented component scores of 50."""
    raw = candidate.entry_quality_breakdown or {}
    required = (
        "price_location",
        "vwap_location",
        "atr_extension",
        "pullback_quality",
        "remaining_reward",
        "support_structure",
        "resistance_structure",
        "short_term_momentum",
        "volume_confirmation",
        "market_alignment",
        "signal_drift",
    )
    missing = [k for k in required if k not in raw]
    if missing:
        raise PretradeRejection(
            "BUY_REJECTED_STALE_DATA",
            f"ENTRY_QUALITY_BREAKDOWN_REQUIRED:{','.join(missing[:4])}",
        )
    breakdown = EntryQualityBreakdown(
        **{k: int(raw[k]) for k in EntryQualityBreakdown.model_fields if k in raw}
    )
    setup_bd = None
    setup_raw = candidate.setup_quality_breakdown or {}
    if setup_raw:
        setup_bd = SetupQualityBreakdown(
            **{k: int(setup_raw[k]) for k in SetupQualityBreakdown.model_fields if k in setup_raw}
        )
    return breakdown, setup_bd


def final_pretrade_validation(
    candidate: TradeCandidate,
    *,
    quote: Quote,
    snapshot: AdmissionSnapshot | None = None,
    bars_count: int | None = None,
    bars: list[Bar] | None = None,
    last_bar_ts: datetime | None = None,
    now: datetime | None = None,
    exec_snap: FeatureSnapshot | None = None,
    market_gate: MarketGateResult | None = None,
    market: MarketAssessment | None = None,
    sector_label: str | None = None,
    sector_tradable: bool | None = None,
    sector_benchmark: str | None = None,
    sector_provider: str | None = None,
    sector_source_ts: datetime | None = None,
    bar_timeframe: str = "1Hour",
    geometry_hash: str | None = None,
    opportunity_id: UUID | None = None,
    decision_version: int = 0,
    tape_last: float | None = None,
) -> tuple[TradeAdmissionResult, AdmissionInput, ZoneArrivalFacts | None]:
    """Re-run admission-side gates with live quote before Risk Engine.

    Builds an immutable AdmissionInput and evaluates only through
    ``evaluate_from_admission_input``. ``now`` is decision_time / evaluated_at.
    Never invents sector_tradable from FRED MarketAssessment.
    """
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    if bars_count is None:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "BARS_REQUIRED")

    th = get_entry_thresholds()
    data = check_data_integrity(
        quote=quote,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        now=evaluated_at,
        require_bars=True,
        quote_max_age_sec=th.quote_max_age_sec,
    )
    if data.status is DataHealthStatus.UNHEALTHY:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", ",".join(data.reason_codes))

    if market_gate is not None and (
        market_gate.status is DataHealthStatus.UNHEALTHY or not market_gate.tradable_long
    ):
        raise PretradeRejection(
            "BUY_REJECTED_REGIME",
            ",".join(market_gate.reason_codes) or "REGIME_BLOCKED",
        )

    bid = float(quote.bid or 0)
    ask = float(quote.ask or 0)
    if bid <= 0 or ask < bid:
        raise PretradeRejection("BUY_REJECTED_SPREAD", "no_top_of_book")

    from trading.entry_spread_gate import evaluate_entry_spread

    spread_gate = evaluate_entry_spread(
        quote,
        now=evaluated_at,
        tape_last=tape_last,
        card_entry=float(candidate.entry),
        thresholds=th,
    )
    if "LIVE_QUOTE_REQUIRED" in spread_gate.reason_codes:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "LIVE_QUOTE_REQUIRED")
    if "QUOTE_STALE" in spread_gate.reason_codes:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "QUOTE_STALE")
    if (
        "SPREAD_TOO_WIDE" in spread_gate.reason_codes
        or "EXTREME_SPREAD" in spread_gate.reason_codes
    ):
        detail = (
            f"spread_bps={spread_gate.bps:.1f}"
            if spread_gate.bps is not None
            else "SPREAD_TOO_WIDE"
        )
        raise PretradeRejection("BUY_REJECTED_SPREAD", detail)

    mid = (bid + ask) / 2

    snap = snapshot
    if snap is None and candidate.admission_snapshot:
        snap = AdmissionSnapshot.model_validate(candidate.admission_snapshot)

    if exec_snap is None:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "FEATURE_SNAPSHOT_REQUIRED")

    setup_type = candidate.setup_type
    if setup_type is SetupType.UNKNOWN:
        raise PretradeRejection("BUY_REJECTED_ADMISSION", "SETUP_TYPE_UNKNOWN")

    zone_low = (
        snap.entry_zone_low
        if snap
        else (float(candidate.entry_zone_low) if candidate.entry_zone_low else None)
    )
    zone_high = (
        snap.entry_zone_high
        if snap
        else (float(candidate.entry_zone_high) if candidate.entry_zone_high else None)
    )
    atr = snap.atr_at_creation if snap else None

    allowed, zone_reasons = entry_allowed_for_setup_type(setup_type, ask, zone_low, zone_high, atr)
    if not allowed:
        raise PretradeRejection("PRICE_OUTSIDE_ENTRY_POLICY", ",".join(zone_reasons))

    ref_price = snap.price_at_creation if snap else float(candidate.entry)
    drift_pct = abs(ask - ref_price) / max(ref_price, 1e-9) * 100.0
    if drift_pct > MAX_APPROVAL_DRIFT_PCT:
        raise PretradeRejection(
            "BUY_REJECTED_PRICE_MOVED",
            f"approval_drift_pct={drift_pct:.2f}",
        )

    setup_q = (
        candidate.setup_quality
        if candidate.setup_quality is not None
        else (snap.setup_quality_at_creation if snap else None)
    )
    entry_q = (
        candidate.entry_quality
        if candidate.entry_quality is not None
        else (snap.entry_quality_at_creation if snap else None)
    )
    has_admission_metadata = bool(
        candidate.admission_version or candidate.admission_snapshot or snap is not None
    )
    if not has_admission_metadata:
        raise PretradeRejection("ADMISSION_REQUIRED", "legacy_candidate_no_admission")
    if setup_q is None or entry_q is None:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "QUALITY_SCORES_REQUIRED")

    if candidate.thesis is None:
        raise PretradeRejection("BUY_REJECTED_ADMISSION", "THESIS_REQUIRED")
    if candidate.thesis is not InstrumentThesis.BULLISH:
        raise PretradeRejection("BUY_REJECTED_ADMISSION", "THESIS_NOT_BULLISH")

    entry_decision = candidate.entry_decision
    if entry_decision is None:
        raise PretradeRejection("BUY_REJECTED_ADMISSION", "ENTRY_DECISION_REQUIRED")

    target_plan = _require_target_plan(candidate)
    if target_plan.reachability is TargetReachabilityClass.UNREALISTIC:
        raise PretradeRejection("NO_TRADE/TARGET_UNREALISTIC", "TARGET_UNREALISTIC")

    breakdown, setup_breakdown = _require_breakdown(candidate)

    facts = evaluate_timing(
        exec_snap,
        signal_price=float(candidate.signal_price or candidate.entry),
        planned_entry=float(candidate.entry),
        planned_stop=float(candidate.stop),
        planned_target=float(candidate.target),
    )
    facts = facts.model_copy(update={"current_price": mid})

    chase = compute_chase_facts(facts, zone_high=zone_high)
    if chase.score >= HARD_CHASE_LIMIT:
        raise PretradeRejection("BUY_REJECTED_CHASE", f"chase={chase.score}")

    bundle = EntryDecisionBundle(
        thesis=candidate.thesis,
        entry_decision=entry_decision,
        entry_quality=entry_q,
        setup_quality=setup_q,
        setup_breakdown=setup_breakdown,
        breakdown=breakdown,
        facts=facts,
        entry_zone_low=candidate.entry_zone_low,
        entry_zone_high=candidate.entry_zone_high,
        stop_price=candidate.stop,
        target=target_plan,
    )

    zone_arrival = None
    if bars and bars_count >= 5 and zone_arrival_required(setup_type):
        from trading.pipeline import zone_arrival_for_admission

        zone_arrival = zone_arrival_for_admission(
            symbol=candidate.symbol,
            candidate=candidate,
            bundle=bundle,
            bars=bars,
        )

    stop_plan = StopPlan(
        price=candidate.stop,
        model=(snap.stop_model if snap and snap.stop_model else "structure"),
        basis_level=snap.structural_level if snap else None,
        reason_codes=[snap.structural_source] if snap and snap.structural_source else [],
    )
    th = get_entry_thresholds()
    gh = geometry_hash or geometry_hash_from_candidate(candidate)
    if not gh:
        raise PretradeRejection("ADMISSION_REQUIRED", "geometry_hash_required")

    admission_input = AdmissionInput(
        bundle=bundle,
        setup_type=setup_type,
        setup_quality=int(setup_q),
        entry_zone_low=candidate.entry_zone_low,
        entry_zone_high=candidate.entry_zone_high,
        stop_plan=stop_plan,
        target_plan=target_plan,
        quote=quote,
        bars_count=bars_count,
        bar_timeframe=bar_timeframe,
        last_bar_ts=last_bar_ts,
        market=market,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
        sector_benchmark=sector_benchmark
        if sector_benchmark is not None
        else (market_gate.benchmark if market_gate else None),
        sector_provider=sector_provider,
        sector_source_ts=sector_source_ts,
        news_status=None,
        earnings_status=None,
        portfolio_snapshot={},
        risk_snapshot={},
        strategy_version=candidate.strategy_version,
        decision_version=decision_version,
        admission_version=candidate.admission_version or ADMISSION_VERSION,
        policy_version=POLICY_VERSION,
        aggressiveness=th.aggressiveness,
        opportunity_id=opportunity_id,
        watch_id=None,
        trigger_version=None,
        geometry_hash=gh,
        evaluated_at=evaluated_at,
        require_bars=True,
    )

    admission = evaluate_from_admission_input(
        admission_input,
        candidate=candidate,
        entry=Decimal(str(round(ask, 4))),
        stop=candidate.stop,
        target=candidate.target,
        zone_arrival=zone_arrival,
        tape_last=tape_last,
    )
    if admission.snapshot is not None:
        admission = admission.model_copy(
            update={
                "snapshot": admission.snapshot.model_copy(
                    update={
                        "quote_ts": quote.ts,
                        "evaluated_at": evaluated_at,
                        "last_bar_ts": last_bar_ts,
                        "market_gate_ts": market_gate.regime_ts if market_gate else None,
                    }
                )
            }
        )
    return require_final_admission(admission), admission_input, zone_arrival
