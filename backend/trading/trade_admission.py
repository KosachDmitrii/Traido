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
from trading.arrival_admission import evaluate_arrival_gate
from trading.chase_facts import HARD_CHASE_LIMIT, compute_chase_facts
from trading.data_integrity import check_data_integrity
from trading.effective_rr import (
    compute_effective_rr,
    price_within_zone_cushion,
    required_admission_rr,
)
from trading.entry_policy import get_entry_thresholds
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
        buffer = (atr or price * 0.01) * ZONE_ABOVE_BUFFER_ATR
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
    cushion_fill: bool = False,
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

    data = check_data_integrity(
        quote=quote,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        now=now,
        require_bars=require_bars,
        quote_max_age_sec=th.quote_max_age_sec,
    )
    if data.status is DataHealthStatus.UNHEALTHY:
        return TradeAdmissionResult(
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

    if quote is None or quote.bid is None or quote.ask is None or quote.ts is None:
        return TradeAdmissionResult(
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

    chase = compute_chase_facts(admission_facts, zone_high=zone_high, thresholds=th)
    structure = evaluate_structural_integrity(
        admission_facts,
        chase_reasons=chase.reason_codes,
        deep_pullback_is_hard=th.pullback_deep_no_trade,
    )
    vetoes: list[str] = []

    if bundle.thesis is not InstrumentThesis.BULLISH:
        reason_codes.append("THESIS_NOT_BULLISH")
        return _result(
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

    allowed, zone_reasons = entry_allowed_for_setup_type(
        st,
        zone_entry_price if zone_entry_price is not None else facts.current_price,
        zone_low,
        zone_high,
        facts.atr,
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
        if (
            cushion_fill
            and in_cushion
            and not stop_valid
            and frozenset(stop_res.reason_codes)
            <= frozenset({"ATR_ONLY_STOP", "INVALID_STOP"})
        ):
            stop_valid = True
            warnings.append("CUSHION_ATR_STOP")
        elif not stop_valid:
            vetoes.extend(vetoes_from_codes(stop_res.reason_codes))
            reason_codes.extend(stop_res.reason_codes)

    if tgt is None or ent is None or stp is None:
        vetoes.append("MISSING_TARGET")
        reason_codes.append("MISSING_TARGET")
        target_valid = False
    else:
        target_res = validate_target(entry=ent, target=tgt, target_plan=tp)
        target_valid = target_res.valid
        if (
            cushion_fill
            and in_cushion
            and not target_valid
            and tp is not None
            and frozenset(target_res.reason_codes) <= frozenset({"TARGET_UNREALISTIC"})
            and float(tgt) == float(tp.price)
        ):
            target_valid = True
            warnings.append("CUSHION_FROZEN_TARGET")
        elif not target_valid:
            vetoes.extend(vetoes_from_codes(target_res.reason_codes))
            reason_codes.extend(target_res.reason_codes)

        rr_res = compute_effective_rr(
            entry=ent,
            stop=stp,
            target=tgt,
            quote=quote,
            zone_low=zone_low,
            zone_high=zone_high,
            atr=facts.atr,
            cushion_atr=ZONE_ABOVE_BUFFER_ATR,
        )
        effective_rr_val = rr_res.effective_rr
        req_rr = required_admission_rr(
            setup_quality=setup_q,
            entry_quality=entry_q,
            chase_score=chase.score,
            structure_valid=structure.valid,
            warnings=warnings,
            min_rr_floor=th.min_effective_rr,
            weak_setup_rr_floor=th.weak_setup_min_rr,
        )
        if effective_rr_val < req_rr:
            vetoes.append("INSUFFICIENT_EFFECTIVE_RR")
            reason_codes.append(f"INSUFFICIENT_EFFECTIVE_RR:{effective_rr_val:.2f}<{req_rr:.2f}")

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

    min_setup = th.min_setup_quality
    min_entry = th.min_entry_quality

    if hard:
        decision = (
            AdmissionDecision.NO_TRADE
            if any(
                v in hard
                for v in (
                    "STRUCTURAL_DAMAGE",
                    "INSUFFICIENT_EFFECTIVE_RR",
                    "MISSING_TARGET",
                    "MISSING_STOP",
                    "MISSING_ENTRY_ZONE",
                    "SETUP_TYPE_UNKNOWN",
                    "INVALID_STOP",
                    "TARGET_UNREALISTIC",
                    "TARGET_PLAN_MISMATCH",
                    "TARGET_NO_BASIS",
                )
            )
            else AdmissionDecision.WAIT
        )
        if "STALE_DATA" in hard or "MARKET_DATA_UNHEALTHY" in hard:
            decision = AdmissionDecision.DATA_BLOCKED
        elif (
            not allowed
            or "ENTRY_OUTSIDE_ALLOWED_ZONE" in hard
            or "EXTREME_CHASE" in hard
            or "SPREAD_TOO_WIDE" in hard
            or "ZONE_ARRIVAL_MISSING" in hard
            or any("ZONE_ARRIVAL" in r or "ARRIVAL_TYPE" in r for r in reason_codes)
        ):
            decision = AdmissionDecision.WAIT
        return _result(
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

    if zone_arrival_required(st) and zone_arrival is None:
        return _result(
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

    if setup_q < min_setup:
        reason_codes.append("SETUP_QUALITY_BELOW_THRESHOLD")
        return _result(
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

    if entry_q < min_entry:
        reason_codes.append("ENTRY_QUALITY_BELOW_THRESHOLD")
        return _result(
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

    if (
        zone_arrival_required(st)
        and zone_arrival is not None
        and arrival_gate is not None
        and arrival_gate.blocked
        and not arrival_gate.hard_veto
    ):
        return _result(
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

    if not allowed:
        return _result(
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

    return TradeAdmissionResult(
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
    )
