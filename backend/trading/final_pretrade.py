"""Final pretrade validation — re-check entry gates at human approve time."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.enums import AdmissionDecision, DataHealthStatus, SetupType
from core.schemas import (
    AdmissionSnapshot,
    FeatureSnapshot,
    Quote,
    TradeAdmissionResult,
    TradeCandidate,
)
from trading.chase_facts import HARD_CHASE_LIMIT, compute_chase_facts
from trading.data_integrity import check_data_integrity
from trading.entry_timing import evaluate_timing
from trading.market_gate import MarketGateResult
from trading.trade_admission import (
    entry_allowed_for_setup_type,
    evaluate_trade_admission,
)

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


def final_pretrade_validation(
    candidate: TradeCandidate,
    *,
    quote: Quote,
    snapshot: AdmissionSnapshot | None = None,
    bars_count: int | None = None,
    last_bar_ts: datetime | None = None,
    now: datetime | None = None,
    exec_snap: FeatureSnapshot | None = None,
    market_gate: MarketGateResult | None = None,
) -> TradeAdmissionResult:
    """Re-run admission-side gates with live quote before Risk Engine.

    ``now`` is decision_time / evaluated_at. Never replaced by quote.ts.
    """
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    if bars_count is None:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "BARS_REQUIRED")

    data = check_data_integrity(
        quote=quote,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        now=evaluated_at,
        require_bars=True,
    )
    if data.status is DataHealthStatus.UNHEALTHY:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", ",".join(data.reason_codes))

    if (
        market_gate is not None
        and (market_gate.status is DataHealthStatus.UNHEALTHY or not market_gate.tradable_long)
    ):
        raise PretradeRejection(
            "BUY_REJECTED_REGIME",
            ",".join(market_gate.reason_codes) or "REGIME_BLOCKED",
        )

    bid = float(quote.bid or 0)
    ask = float(quote.ask or 0)
    if bid <= 0 or ask < bid:
        raise PretradeRejection("BUY_REJECTED_SPREAD", "no_top_of_book")

    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10000
    max_spread = float(getattr(candidate, "max_spread_bps", 30) or 30)
    if spread_bps > max_spread * 2:
        raise PretradeRejection("BUY_REJECTED_SPREAD", f"spread_bps={spread_bps:.1f}")

    snap = snapshot
    if snap is None and candidate.admission_snapshot:
        snap = AdmissionSnapshot.model_validate(candidate.admission_snapshot)

    if exec_snap is None:
        raise PretradeRejection("BUY_REJECTED_STALE_DATA", "FEATURE_SNAPSHOT_REQUIRED")

    setup_type = candidate.setup_type
    zone_low = snap.entry_zone_low if snap else (
        float(candidate.entry_zone_low) if candidate.entry_zone_low else None
    )
    zone_high = snap.entry_zone_high if snap else (
        float(candidate.entry_zone_high) if candidate.entry_zone_high else None
    )
    atr = snap.atr_at_creation if snap else None

    allowed, zone_reasons = entry_allowed_for_setup_type(
        setup_type, mid, zone_low, zone_high, atr
    )
    if not allowed and setup_type is not SetupType.UNKNOWN:
        raise PretradeRejection("PRICE_OUTSIDE_ENTRY_POLICY", ",".join(zone_reasons))

    ref_price = snap.price_at_creation if snap else float(candidate.entry)
    drift_pct = abs(mid - ref_price) / max(ref_price, 1e-9) * 100.0
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

    facts = evaluate_timing(
        exec_snap,
        signal_price=float(candidate.signal_price or candidate.entry),
        planned_entry=float(candidate.entry),
        planned_stop=float(candidate.stop),
        planned_target=float(candidate.target),
    )
    facts = facts.model_copy(update={"current_price": mid})
    # Preserve structural stop basis from the admission snapshot when features
    # did not re-derive nearest support (flat synthetic series in tests, etc.).
    if facts.nearest_support is None and snap and snap.stop_at_creation is not None:
        facts = facts.model_copy(update={"nearest_support": float(snap.stop_at_creation)})
    chase = compute_chase_facts(facts, zone_high=zone_high)
    if chase.score >= HARD_CHASE_LIMIT:
        raise PretradeRejection("BUY_REJECTED_CHASE", f"chase={chase.score}")

    from core.enums import EntryDecision, InstrumentThesis, TargetReachabilityClass
    from core.schemas import (
        EntryDecisionBundle,
        EntryQualityBreakdown,
        SetupQualityBreakdown,
        TargetPlan,
    )

    # Prefer candidate's target plan metadata when present; never invent 2R structure.
    target_plan = TargetPlan(
        price=candidate.target,
        model=getattr(candidate, "target_model", None) or "structure",
        reachability=TargetReachabilityClass.REALISTIC,
    )
    bundle = EntryDecisionBundle(
        thesis=candidate.thesis or InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=entry_q,
        setup_quality=setup_q,
        setup_breakdown=SetupQualityBreakdown(),
        breakdown=EntryQualityBreakdown(
            price_location=50,
            vwap_location=50,
            atr_extension=50,
            pullback_quality=50,
            remaining_reward=50,
            support_structure=50,
            resistance_structure=50,
            short_term_momentum=50,
            volume_confirmation=50,
            market_alignment=50,
            signal_drift=50,
        ),
        facts=facts,
        entry_zone_low=candidate.entry_zone_low,
        entry_zone_high=candidate.entry_zone_high,
        stop_price=candidate.stop,
        target=target_plan,
    )
    admission = evaluate_trade_admission(
        bundle=bundle,
        candidate=candidate,
        quote=quote,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        require_bars=True,
        entry=Decimal(str(round(ask, 4))),
        stop=candidate.stop,
        target=candidate.target,
        target_plan=target_plan,
        now=evaluated_at,
    )
    # Stamp decision-time facts onto snapshot for the approval record.
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
    return require_final_admission(admission)
