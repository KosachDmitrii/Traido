"""Trade Admission — sole authority for BUY_ALLOWED.

Facts and evaluations arrive as inputs; this module applies gates only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.enums import AdmissionDecision, DataHealthStatus, InstrumentThesis, SetupType
from core.schemas import (
    AdmissionSnapshot,
    EntryDecisionBundle,
    EntryTimingFacts,
    Quote,
    StructuralIntegrityFacts,
    TargetPlan,
    TradeAdmissionResult,
    TradeCandidate,
)
from trading.chase_facts import HARD_CHASE_LIMIT, compute_chase_facts
from trading.data_integrity import check_data_integrity
from trading.effective_rr import compute_effective_rr, required_admission_rr
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
        if zone_low is None and zone_high is None:
            return True, []
        return False, ["SETUP_TYPE_UNKNOWN"]
    if setup_type is SetupType.PULLBACK_CONTINUATION:
        if zone_high is None:
            return False, ["MISSING_ENTRY_ZONE"]
        buffer = (atr or price * 0.01) * ZONE_ABOVE_BUFFER_ATR
        if price > zone_high + buffer:
            return False, ["ENTRY_OUTSIDE_ALLOWED_ZONE"]
    return True, []


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

    ent = entry or (candidate.entry if candidate else facts.current_price)
    stp = stop or bundle.stop_price or (candidate.stop if candidate else None)
    tgt = target or (bundle.target.price if bundle.target else (candidate.target if candidate else None))
    tp = target_plan or bundle.target

    zone_low = float(bundle.entry_zone_low) if bundle.entry_zone_low else None
    zone_high = float(bundle.entry_zone_high) if bundle.entry_zone_high else None
    if candidate and candidate.entry_zone_low is not None:
        zone_low = float(candidate.entry_zone_low)
    if candidate and candidate.entry_zone_high is not None:
        zone_high = float(candidate.entry_zone_high)

    data = check_data_integrity(
        quote=quote,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        now=now,
        require_bars=require_bars,
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

    chase = compute_chase_facts(facts, zone_high=zone_high, thresholds=th)
    structure = evaluate_structural_integrity(facts, chase_reasons=chase.reason_codes)
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
        st, facts.current_price, zone_low, zone_high, facts.atr
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

    stop_valid = True
    target_valid = True
    effective_rr_val: float | None = None

    if stp is not None and ent is not None:
        stop_res = validate_stop(entry=ent, stop=stp, facts=facts)
        stop_valid = stop_res.valid
        if not stop_valid:
            vetoes.extend(vetoes_from_codes(stop_res.reason_codes))
            reason_codes.extend(stop_res.reason_codes)

    if tgt is not None and ent is not None and stp is not None:
        target_res = validate_target(entry=ent, target=tgt, target_plan=tp)
        target_valid = target_res.valid
        if not target_valid:
            vetoes.extend(vetoes_from_codes(target_res.reason_codes))
            reason_codes.extend(target_res.reason_codes)

        rr_res = compute_effective_rr(entry=ent, stop=stp, target=tgt, quote=quote)
        effective_rr_val = rr_res.effective_rr
        req_rr = required_admission_rr(
            setup_quality=setup_q,
            entry_quality=entry_q,
            chase_score=chase.score,
            structure_valid=structure.valid,
            warnings=warnings,
        )
        if effective_rr_val < req_rr:
            vetoes.append("INSUFFICIENT_EFFECTIVE_RR")
            reason_codes.append(
                f"INSUFFICIENT_EFFECTIVE_RR:{effective_rr_val:.2f}<{req_rr:.2f}"
            )

    if quote is not None:
        bid = float(quote.bid or 0)
        ask = float(quote.ask or 0)
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10000
            if spread_bps > th.max_spread_bps * 2:
                vetoes.append("EXTREME_SPREAD")
                reason_codes.append("EXTREME_SPREAD")

    if zone_arrival_required(st) and zone_arrival is not None:
        if zone_arrival.crash_velocity:
            vetoes.append("CRASH_VELOCITY")
            reason_codes.extend(zone_arrival.reason_codes[:4])
        if zone_arrival.structural_damage:
            vetoes.append("STRUCTURAL_DAMAGE")
            reason_codes.extend(zone_arrival.reason_codes[:4])
        min_arrival = th.min_zone_arrival_quality
        if zone_arrival.score < min_arrival and (
            not th.allow_fast_pullback or zone_arrival.arrival_type.value != "FAST_PULLBACK"
        ):
            reason_codes.append(f"ZONE_ARRIVAL_QUALITY_LOW:{int(zone_arrival.score)}")
        if (
            zone_arrival.arrival_type.value
            in {"SELL_OFF", "CRASH", "GAP_DOWN", "STRUCTURAL_BREAK"}
            and not th.allow_fast_pullback
        ):
            reason_codes.append(f"ARRIVAL_TYPE_{zone_arrival.arrival_type.value}")

    vetoes = list(dict.fromkeys(vetoes))
    hard = vetoes_from_codes(vetoes + reason_codes)

    min_setup = th.min_setup_quality
    min_entry = th.min_entry_quality

    if hard:
        decision = AdmissionDecision.NO_TRADE if any(
            v in hard for v in ("STRUCTURAL_DAMAGE", "INSUFFICIENT_EFFECTIVE_RR", "MISSING_TARGET", "INVALID_STOP", "TARGET_UNREALISTIC")
        ) else AdmissionDecision.WAIT
        if "STALE_DATA" in hard or "MARKET_DATA_UNHEALTHY" in hard:
            decision = AdmissionDecision.DATA_BLOCKED
        elif not allowed or "ENTRY_OUTSIDE_ALLOWED_ZONE" in hard or "EXTREME_CHASE" in hard or any("ZONE_ARRIVAL" in r or "ARRIVAL_TYPE" in r for r in reason_codes):
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

    if zone_arrival_required(st) and zone_arrival is not None:
        arrival_blocked = zone_arrival.score < th.min_zone_arrival_quality
        if th.allow_fast_pullback and zone_arrival.arrival_type.value == "FAST_PULLBACK":
            arrival_blocked = zone_arrival.score < max(45, th.min_zone_arrival_quality - 15)
        if arrival_blocked and not any(
            v in vetoes for v in ("CRASH_VELOCITY", "STRUCTURAL_DAMAGE")
        ):
            if f"ZONE_ARRIVAL_QUALITY_LOW:{int(zone_arrival.score)}" not in reason_codes:
                reason_codes.append(f"ZONE_ARRIVAL_QUALITY_LOW:{int(zone_arrival.score)}")
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

    snapshot = build_admission_snapshot(
        facts=facts,
        setup_type=st,
        setup_quality=setup_q,
        entry_quality=entry_q,
        entry=ent,
        stop=stp or facts.current_price * 0.95,
        target=tgt or facts.current_price * 1.05,
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