"""Trade Admission — sole authority for BUY_ALLOWED.

Facts and evaluations arrive as inputs; this module applies gates only.
Missing stop/target/setup/zone never invents geometry — it fails closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.enums import AdmissionDecision, DataHealthStatus, InstrumentThesis, SetupType
from core.schemas import (
    AdmissionInput,
    AdmissionSnapshot,
    EntryDecisionBundle,
    EntryTimingFacts,
    Quote,
    StructuralIntegrityFacts,
    TargetPlan,
    TradeAdmissionResult,
    TradeCandidate,
)
from trading import admission_relaxation
from trading.admission_relaxation import emit_relaxation_observation
from trading.arrival_admission import evaluate_arrival_gate
from trading.buy_confirmation import (
    BUY_READY_CANDIDATE,
    NOT_BUY_READY,
    buy_confirmation_for,
    evaluate_buy_confirmation,
    evaluate_buy_ready,
)
from trading.chase_facts import HARD_CHASE_LIMIT, compute_chase_facts
from trading.data_integrity import check_data_integrity
from trading.decision_precedence import resolve_admission_decision
from trading.effective_rr import (
    compute_effective_rr,
    planned_long_rr,
    price_within_zone_cushion,
)
from trading.entry_policy import get_entry_thresholds
from trading.execution_geometry import resolve_capital_atr
from trading.stop_validation import validate_stop
from trading.structural_integrity import evaluate_structural_integrity
from trading.target_validation import validate_target
from trading.trade_vetoes import vetoes_from_codes
from trading.zone_arrival import ZoneArrivalFacts, zone_arrival_required

ADMISSION_VERSION = "admission@1.1.0"
POLICY_VERSION = "entry_policy@1"

# Above zone high by this many ATR → outside allowed pullback entry.
ZONE_ABOVE_BUFFER_ATR = 0.20


def build_admission_snapshot(
    *,
    facts: EntryTimingFacts,
    setup_type: SetupType,
    setup_quality: int,
    entry_quality: int,
    entry: Decimal | float,
    stop: Decimal | float,
    target: Decimal | float,
    zone_low: float | None,
    zone_high: float | None,
    effective_rr: float | None,
    aggressiveness: int | None = None,
) -> AdmissionSnapshot:
    th = get_entry_thresholds()
    vwap = None
    if facts.distance_from_vwap_pct is not None and facts.current_price > 0:
        vwap = facts.current_price / (1 + facts.distance_from_vwap_pct / 100.0)
    return AdmissionSnapshot(
        price_at_creation=facts.current_price,
        atr_at_creation=facts.atr,
        vwap_at_creation=vwap,
        setup_type=setup_type,
        entry_zone_low=zone_low,
        entry_zone_high=zone_high,
        setup_quality_at_creation=setup_quality,
        entry_quality_at_creation=entry_quality,
        stop_at_creation=float(stop),
        target_at_creation=float(target),
        entry_at_creation=float(entry),
        effective_rr_at_creation=effective_rr,
        policy_version=POLICY_VERSION,
        aggressiveness=aggressiveness if aggressiveness is not None else th.aggressiveness,
        admission_version=ADMISSION_VERSION,
        created_at=datetime.now(UTC),
    )


def entry_allowed_for_setup_type(
    setup_type: SetupType,
    price: float,
    zone_low: float | None,
    zone_high: float | None,
    atr: float | None,
) -> tuple[bool, list[str]]:
    if setup_type is SetupType.UNKNOWN:
        return False, ["SETUP_TYPE_UNKNOWN"]
    if setup_type is SetupType.PULLBACK_CONTINUATION:
        if zone_low is None or zone_high is None:
            return False, ["MISSING_ENTRY_ZONE"]
        atr_v = resolve_capital_atr(facts_atr=atr)
        if atr_v is None:
            return False, ["MISSING_ATR"]
        buffer = atr_v * ZONE_ABOVE_BUFFER_ATR
        if price > zone_high + buffer:
            return False, ["ENTRY_OUTSIDE_ALLOWED_ZONE"]
        # Deep undercut: wait for reclaim of the zone, not BUY at the print.
        if price < zone_low - buffer:
            return False, ["ENTRY_OUTSIDE_ALLOWED_ZONE"]
    return True, []


def evaluate_from_admission_input(
    admission_input: AdmissionInput,
    *,
    candidate: TradeCandidate | None = None,
    zone_arrival: ZoneArrivalFacts | None = None,
    entry: Decimal | float | None = None,
    stop: Decimal | float | None = None,
    target: Decimal | float | None = None,
    tape_last: float | None = None,
) -> TradeAdmissionResult:
    """Evaluate admission from an immutable AdmissionInput — preferred capital path."""
    return evaluate_trade_admission(
        bundle=admission_input.bundle,
        candidate=candidate,
        setup_type=admission_input.setup_type,
        quote=admission_input.quote,
        bars_count=admission_input.bars_count,
        last_bar_ts=admission_input.last_bar_ts,
        require_bars=admission_input.require_bars,
        entry=entry or admission_input.bundle.facts.current_price,
        stop=stop
        or (admission_input.stop_plan.price if admission_input.stop_plan else None)
        or admission_input.bundle.stop_price,
        target=target or admission_input.target_plan.price,
        target_plan=admission_input.target_plan,
        zone_arrival=zone_arrival,
        now=admission_input.evaluated_at,
        tape_last=tape_last,
        stop_plan_model=admission_input.stop_plan.model if admission_input.stop_plan else None,
        stop_structural_source=(
            admission_input.stop_plan.reason_codes[0]
            if admission_input.stop_plan and admission_input.stop_plan.reason_codes
            else None
        ),
        stop_structural_level=(
            admission_input.stop_plan.basis_level if admission_input.stop_plan else None
        ),
    )


def evaluate_trade_admission(
    *,
    bundle: EntryDecisionBundle,
    candidate: TradeCandidate | None = None,
    setup_type: SetupType | None = None,
    quote: Quote | None = None,
    bars_count: int | None = None,
    last_bar_ts: datetime | None = None,
    require_bars: bool = False,
    entry: Decimal | float | None = None,
    stop: Decimal | float | None = None,
    target: Decimal | float | None = None,
    target_plan: TargetPlan | None = None,
    zone_arrival: ZoneArrivalFacts | None = None,
    now: datetime | None = None,
    tape_last: float | None = None,
    stop_plan_model: str | None = None,
    stop_structural_source: str | None = None,
    stop_structural_level: float | None = None,
    zone_entry_price: float | None = None,
    regime_allowed: bool = True,
) -> TradeAdmissionResult:
    """Apply all trading gates. Does not re-run quant indicators."""
    th = get_entry_thresholds()
    now = now or datetime.now(UTC)
    facts = bundle.facts
    st = setup_type or (candidate.setup_type if candidate else SetupType.UNKNOWN)
    setup_q = bundle.setup_quality or (
        candidate.setup_quality if candidate and candidate.setup_quality is not None else 0
    )
    entry_q = bundle.entry_quality
    warnings: list[str] = []
    reason_codes: list[str] = []

    ent = entry if entry is not None else (candidate.entry if candidate else facts.current_price)
    if stop is not None:
        stp = stop
    elif bundle.stop_price is not None:
        stp = bundle.stop_price
    elif candidate is not None:
        stp = candidate.stop
    else:
        stp = None
    if target is not None:
        tgt = target
    elif bundle.target is not None:
        tgt = bundle.target.price
    elif candidate is not None:
        tgt = candidate.target
    else:
        tgt = None
    tp = target_plan or bundle.target

    zone_low = float(bundle.entry_zone_low) if bundle.entry_zone_low else None
    zone_high = float(bundle.entry_zone_high) if bundle.entry_zone_high else None
    if candidate and candidate.entry_zone_low is not None:
        zone_low = float(candidate.entry_zone_low)
    if candidate and candidate.entry_zone_high is not None:
        zone_high = float(candidate.entry_zone_high)

    zone_check_price = (
        zone_entry_price if zone_entry_price is not None else float(facts.current_price)
    )
    in_cushion = price_within_zone_cushion(
        price=zone_check_price,
        zone_low=zone_low,
        zone_high=zone_high,
        atr=facts.atr,
        cushion_atr=ZONE_ABOVE_BUFFER_ATR,
    )
    admission_facts = facts
    if in_cushion and ent is not None:
        admission_facts = facts.model_copy(update={"current_price": float(ent)})

    symbol = (
        (quote.symbol if quote is not None and quote.symbol else None)
        or (candidate.symbol if candidate is not None else None)
        or "UNKNOWN"
    )
    planned_rr = (
        planned_long_rr(ent, stp, tgt)
        if ent is not None and stp is not None and tgt is not None
        else None
    )
    compensation_eligible = False
    compensation_applied = False
    reached_admission = False
    hard_risk_block = False
    buy_ready_flag = False
    confirmation_failed = False
    confirmation_relaxed = False

    def finish(
        result: TradeAdmissionResult,
        *,
        reached: bool | None = None,
    ) -> TradeAdmissionResult:
        emit_relaxation_observation(
            symbol=str(symbol),
            relaxation_level=th.aggressiveness,
            setup_score=setup_q,
            setup_floor=th.min_setup_quality,
            entry_score=entry_q,
            entry_floor=th.min_entry_quality,
            price_in_entry_zone=in_cushion,
            rr=planned_rr,
            compensation_eligible=compensation_eligible,
            compensation_applied=compensation_applied,
            result=result,
            regime_allowed=regime_allowed,
            hard_risk_block=hard_risk_block,
            reached_admission=reached_admission if reached is None else reached,
            buy_ready=buy_ready_flag,
            confirmation_failed=confirmation_failed,
            confirmation_relaxed=confirmation_relaxed,
        )
        return result

    data = check_data_integrity(
        quote=quote,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        now=now,
        require_bars=require_bars,
        quote_max_age_sec=th.quote_max_age_sec,
    )
    if data.status is DataHealthStatus.UNHEALTHY:
        return finish(
            TradeAdmissionResult(
                decision=AdmissionDecision.DATA_BLOCKED,
                admitted=False,
                setup_type=st,
                setup_quality=setup_q,
                entry_quality=entry_q,
                data_status=data.status,
                vetoes=vetoes_from_codes(data.reason_codes),
                reason_codes=[*data.reason_codes, "DATA_BLOCKED"],
                admission_version=ADMISSION_VERSION,
            )
        )

    if quote is None or quote.bid is None or quote.ask is None or quote.ts is None:
        return finish(
            TradeAdmissionResult(
                decision=AdmissionDecision.DATA_BLOCKED,
                admitted=False,
                setup_type=st,
                setup_quality=setup_q,
                entry_quality=entry_q,
                data_status=DataHealthStatus.UNHEALTHY,
                vetoes=["STALE_DATA"],
                reason_codes=["QUOTE_INCOMPLETE", "DATA_BLOCKED"],
                admission_version=ADMISSION_VERSION,
            )
        )

    snap_atr_raw = None
    if (
        candidate
        and candidate.admission_snapshot
        and isinstance(candidate.admission_snapshot, dict)
    ):
        raw = candidate.admission_snapshot.get("atr_at_creation")
        if isinstance(raw, (int, float)) and raw > 0:
            snap_atr_raw = float(raw)
    capital_atr = resolve_capital_atr(facts_atr=facts.atr, snapshot_atr=snap_atr_raw)
    if capital_atr is None and st in {
        SetupType.PULLBACK_CONTINUATION,
        SetupType.VWAP_RECLAIM,
        SetupType.MEAN_REVERSION,
        SetupType.BREAKOUT_CONTINUATION,
    }:
        return finish(
            TradeAdmissionResult(
                decision=AdmissionDecision.DATA_BLOCKED,
                admitted=False,
                setup_type=st,
                setup_quality=setup_q,
                entry_quality=entry_q,
                data_status=data.status,
                vetoes=["MISSING_ATR"],
                reason_codes=["MISSING_ATR", "DATA_BLOCKED"],
                admission_version=ADMISSION_VERSION,
            )
        )

    chase = compute_chase_facts(admission_facts, zone_high=zone_high, thresholds=th)
    structure = evaluate_structural_integrity(
        admission_facts,
        chase_reasons=chase.reason_codes,
        deep_pullback_is_hard=th.pullback_deep_no_trade,
    )
    vetoes: list[str] = []

    reached_admission = True

    if bundle.thesis is not InstrumentThesis.BULLISH:
        reason_codes.append("THESIS_NOT_BULLISH")
        return finish(
            _result(
                AdmissionDecision.NO_TRADE,
                st,
                setup_q,
                entry_q,
                chase.score,
                structure,
                data.status,
                vetoes,
                warnings,
                reason_codes,
            )
        )

    allowed, zone_reasons = entry_allowed_for_setup_type(
        st,
        zone_entry_price if zone_entry_price is not None else facts.current_price,
        zone_low,
        zone_high,
        capital_atr or facts.atr,
    )
    reason_codes.extend(zone_reasons)
    if not allowed:
        vetoes.extend(vetoes_from_codes(zone_reasons))

    if chase.score >= HARD_CHASE_LIMIT:
        vetoes.append("EXTREME_CHASE")
        reason_codes.append("EXTREME_CHASE")

    if structure.hard_damage:
        vetoes.append("STRUCTURAL_DAMAGE")
        reason_codes.extend(structure.reason_codes)

    stop_valid = False
    target_valid = False
    effective_rr_val: float | None = None

    if stp is None or ent is None:
        vetoes.append("MISSING_STOP")
        reason_codes.append("MISSING_STOP")
        stop_valid = False
    else:
        snap_model = stop_plan_model
        snap_source = stop_structural_source
        snap_level = stop_structural_level
        if candidate and candidate.admission_snapshot:
            snap = candidate.admission_snapshot
            if isinstance(snap, dict):
                snap_model = snap_model or snap.get("stop_model")
                snap_source = snap_source or snap.get("structural_source")
                level = snap.get("structural_level")
                if snap_level is None and level is not None:
                    snap_level = float(level)
        stop_res = validate_stop(
            entry=ent,
            stop=stp,
            facts=admission_facts,
            stop_model=snap_model,
            structural_source=snap_source,
            structural_level=snap_level,
        )
        stop_valid = stop_res.valid
        if not stop_valid:
            vetoes.extend(vetoes_from_codes(stop_res.reason_codes))
            reason_codes.extend(stop_res.reason_codes)

    if tgt is None or ent is None or stp is None:
        vetoes.append("MISSING_TARGET")
        reason_codes.append("MISSING_TARGET")
        target_valid = False
    else:
        target_res = validate_target(entry=ent, target=tgt, target_plan=tp)
        target_valid = target_res.valid
        if not target_valid:
            vetoes.extend(vetoes_from_codes(target_res.reason_codes))
            reason_codes.extend(target_res.reason_codes)

        rr_res = compute_effective_rr(
            entry=ent,
            stop=stp,
            target=tgt,
            quote=quote,
            zone_low=zone_low,
            zone_high=zone_high,
            atr=capital_atr or facts.atr,
            cushion_atr=ZONE_ABOVE_BUFFER_ATR,
        )
        effective_rr_val = rr_res.effective_rr
        # Effective R:R vs the slider is applied in the confirmation layer.

    bid = float(quote.bid or 0)
    ask = float(quote.ask or 0)
    if bid > 0 and ask >= bid:
        from trading.entry_spread_gate import evaluate_entry_spread

        spread_gate = evaluate_entry_spread(
            quote,
            now=now,
            tape_last=tape_last,
            facts_price=zone_entry_price,
            card_entry=float(ent) if ent is not None else None,
            thresholds=th,
        )
        if "SPREAD_TOO_WIDE" in spread_gate.reason_codes:
            reason_codes.append("SPREAD_TOO_WIDE")
        if spread_gate.extreme:
            vetoes.append("EXTREME_SPREAD")
            reason_codes.append("EXTREME_SPREAD")

    arrival_gate = None
    if zone_arrival_required(st):
        if zone_arrival is None:
            reason_codes.append("ZONE_ARRIVAL_MISSING")
            vetoes.append("ZONE_ARRIVAL_MISSING")
        else:
            arrival_gate = evaluate_arrival_gate(zone_arrival, th)
            warnings.extend(arrival_gate.warnings)
            reason_codes.extend(arrival_gate.reason_codes)
            vetoes.extend(arrival_gate.veto_codes)
    elif zone_arrival is not None:
        arrival_gate = evaluate_arrival_gate(zone_arrival, th)
        warnings.extend(arrival_gate.warnings)
        reason_codes.extend(arrival_gate.reason_codes)
        vetoes.extend(arrival_gate.veto_codes)

    vetoes = list(dict.fromkeys(vetoes))
    hard = vetoes_from_codes(vetoes + reason_codes)

    if hard:
        hard_risk_block = True
        decision = resolve_admission_decision(hard, reason_codes, zone_allowed=allowed)
        return finish(
            _result(
                decision,
                st,
                setup_q,
                entry_q,
                chase.score,
                structure,
                data.status,
                hard,
                warnings,
                reason_codes,
                effective_rr=effective_rr_val,
                stop_valid=stop_valid,
                target_valid=target_valid,
            )
        )

    if zone_arrival_required(st) and zone_arrival is None:
        reason_codes.append("WAITING_CONFIRMATION")
        return finish(
            _result(
                AdmissionDecision.WAIT,
                st,
                setup_q,
                entry_q,
                chase.score,
                structure,
                data.status,
                vetoes,
                warnings,
                reason_codes,
                effective_rr=effective_rr_val,
                stop_valid=stop_valid,
                target_valid=target_valid,
            )
        )

    ready = evaluate_buy_ready(
        candidate_exists=True,
        structurally_valid=not structure.hard_damage,
        price_in_entry_zone=in_cushion and allowed,
        stop_valid=stop_valid,
        target_valid=target_valid,
        planned_rr=planned_rr,
        data_fresh=True,
        regime_allowed=regime_allowed,
        hard_veto=False,
        setup_quality=setup_q,
        entry_quality=entry_q,
        thesis_bullish=bundle.thesis is InstrumentThesis.BULLISH,
    )
    reason_codes.extend(ready.reason_codes)
    if not ready.ready:
        if NOT_BUY_READY not in reason_codes:
            reason_codes.append(NOT_BUY_READY)
        blocked = ready.blocked_decision or AdmissionDecision.WAIT
        return finish(
            _result(
                blocked,
                st,
                setup_q,
                entry_q,
                chase.score,
                structure,
                data.status,
                vetoes,
                warnings,
                reason_codes,
                effective_rr=effective_rr_val,
                stop_valid=stop_valid,
                target_valid=target_valid,
            )
        )

    buy_ready_flag = True
    if BUY_READY_CANDIDATE not in reason_codes:
        reason_codes.append(BUY_READY_CANDIDATE)

    policy = buy_confirmation_for(th.buy_confirmation_strictness)
    confirm = evaluate_buy_confirmation(
        policy=policy,
        setup_quality=setup_q,
        entry_quality=entry_q,
        planned_rr=planned_rr,
        effective_rr=effective_rr_val,
        momentum_pct=facts.short_term_momentum_pct,
        pullback_vol_ratio=facts.pullback_vol_ratio,
        price=float(facts.current_price),
        distance_from_vwap_pct=facts.distance_from_vwap_pct,
        anchor_price=facts.anchor_price,
        structure_valid=structure.valid,
        paper=admission_relaxation.is_paper_broker(),
    )
    reason_codes.extend(confirm.reason_codes)
    warnings.extend(confirm.warnings)
    compensation_applied = "SETUP_COMPENSATED" in confirm.reason_codes
    compensation_eligible = compensation_applied
    confirmation_relaxed = confirm.relaxed

    arrival_soft_block = (
        zone_arrival_required(st)
        and zone_arrival is not None
        and arrival_gate is not None
        and arrival_gate.blocked
        and not arrival_gate.hard_veto
    )
    if arrival_soft_block:
        reason_codes.append("WAITING_CONFIRMATION")

    if not confirm.passed or arrival_soft_block:
        confirmation_failed = True
        if "WAITING_CONFIRMATION" not in reason_codes:
            reason_codes.append("WAITING_CONFIRMATION")
        return finish(
            _result(
                AdmissionDecision.WAIT,
                st,
                setup_q,
                entry_q,
                chase.score,
                structure,
                data.status,
                vetoes,
                warnings,
                reason_codes,
                effective_rr=effective_rr_val,
                stop_valid=stop_valid,
                target_valid=target_valid,
                buy_ready=True,
                confirmation_relaxed=confirm.relaxed,
            )
        )

    if data.status is DataHealthStatus.DEGRADED:
        warnings.append("DATA_DEGRADED")

    # BUY_ALLOWED requires real stop and target — never synthesize ±5%.
    assert stp is not None and tgt is not None and ent is not None
    snapshot = build_admission_snapshot(
        facts=facts,
        setup_type=st,
        setup_quality=setup_q,
        entry_quality=entry_q,
        entry=ent,
        stop=stp,
        target=tgt,
        zone_low=zone_low,
        zone_high=zone_high,
        effective_rr=effective_rr_val,
    )

    return finish(
        TradeAdmissionResult(
            decision=AdmissionDecision.BUY_ALLOWED,
            admitted=True,
            setup_type=st,
            setup_quality=setup_q,
            entry_quality=entry_q,
            effective_rr=effective_rr_val,
            chase_score=chase.score,
            structure_valid=structure.valid,
            stop_valid=stop_valid,
            target_valid=target_valid,
            data_status=data.status,
            vetoes=[],
            warnings=warnings,
            reason_codes=[*reason_codes, "BUY_ALLOWED"],
            admission_version=ADMISSION_VERSION,
            snapshot=snapshot,
            buy_ready=True,
            confirmation_relaxed=confirm.relaxed,
        )
    )


def _result(
    decision: AdmissionDecision,
    setup_type: SetupType,
    setup_quality: int,
    entry_quality: int,
    chase_score: int,
    structure: StructuralIntegrityFacts,
    data_status: DataHealthStatus,
    vetoes: list[str],
    warnings: list[str],
    reason_codes: list[str],
    *,
    effective_rr: float | None = None,
    stop_valid: bool = True,
    target_valid: bool = True,
    buy_ready: bool = False,
    confirmation_relaxed: bool = False,
) -> TradeAdmissionResult:
    return TradeAdmissionResult(
        decision=decision,
        admitted=decision is AdmissionDecision.BUY_ALLOWED,
        setup_type=setup_type,
        setup_quality=setup_quality,
        entry_quality=entry_quality,
        effective_rr=effective_rr,
        chase_score=chase_score,
        structure_valid=structure.valid,
        stop_valid=stop_valid,
        target_valid=target_valid,
        data_status=data_status,
        vetoes=list(dict.fromkeys(vetoes)),
        warnings=warnings,
        reason_codes=reason_codes,
        admission_version=ADMISSION_VERSION,
        buy_ready=buy_ready,
        confirmation_relaxed=confirmation_relaxed,
    )
