"""Watch revalidation must publish the same full geometry admission checked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import (
    AdmissionDecision,
    EntryWatchStatus,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
    TradeAction,
)
from core.schemas import (
    AdmissionSnapshot,
    EntryWatch,
    Quote,
    TargetPlan,
    TradeAdmissionResult,
    TradeCandidate,
)
from trading.entry_watch_eval import RevalidationBuildResult, build_candidate_from_revalidation
from trading.execution_geometry import (
    ExecutionGeometry,
    executable_geometry_for_watch,
    normalize_geometry_price,
    validate_geometry_against_admission_snapshot,
)
from trading.geometry_hash import compute_geometry_hash, geometry_hash_from_candidate


def _quote(*, ask: str = "100.05", bid: str = "100.00") -> Quote:
    return Quote(
        symbol="TEST",
        bid=Decimal(bid),
        ask=Decimal(ask),
        ts=datetime.now(UTC),
        source="test",
    )


def _watch(*, planned_target: Decimal = Decimal(103)) -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="TEST",
        strategy_version="test",
        exec_timeframe="H1",
        created_at=now,
        valid_until=now + timedelta(hours=2),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(100),
        current_price_at_creation=Decimal(100),
        last_price=Decimal(100),
        entry_zone_low=Decimal("99.5"),
        entry_zone_high=Decimal("100.5"),
        planned_entry=Decimal(100),
        planned_stop=Decimal("98.5"),
        planned_target=planned_target,
        entry_quality_at_creation=70,
        status=EntryWatchStatus.TRIGGERED,
        reasons=[],
    )


def _base_candidate(watch: EntryWatch) -> TradeCandidate:
    return TradeCandidate(
        symbol=watch.symbol,
        action=TradeAction.BUY,
        confidence=0.8,
        entry=watch.planned_entry,
        stop=watch.planned_stop,
        target=watch.planned_target,
        risk_reward=2.0,
        reasons=["base"],
        strategy_version=watch.strategy_version,
        thesis=watch.thesis,
    )


def _admission_for(
    *,
    target: float,
    stop: float = 98.5,
    entry: float = 100.05,
    include_entry_at_creation: bool = True,
) -> TradeAdmissionResult:
    snap = AdmissionSnapshot(
        price_at_creation=entry,
        atr_at_creation=1.0,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=70,
        entry_quality_at_creation=70,
        stop_at_creation=stop,
        target_at_creation=target,
        entry_at_creation=entry if include_entry_at_creation else None,
        effective_rr_at_creation=2.0,
    )
    return TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=70,
        entry_quality=70,
        effective_rr=2.0,
        chase_score=10,
        structure_valid=True,
        stop_valid=True,
        target_valid=True,
        reason_codes=["BUY_ALLOWED"],
        snapshot=snap,
    )


def test_executable_geometry_uses_recalculated_target_not_planned() -> None:
    watch = _watch(planned_target=Decimal(103))
    target_plan = TargetPlan(
        price=Decimal("105.5"),
        model="structure",
        reachability=TargetReachabilityClass.REALISTIC,
        two_r_target=Decimal(103),
    )
    quote = _quote()
    geometry = executable_geometry_for_watch(watch, quote=quote, target_plan=target_plan, atr=1.0)
    assert geometry.target == Decimal("105.5")
    assert geometry.target != watch.planned_target


def test_entry_mismatch_blocks_candidate() -> None:
    watch = _watch()
    admission = _admission_for(target=105.5, entry=100.05)
    quote = _quote(ask="101.00", bid="100.95")
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
    )
    assert isinstance(result, RevalidationBuildResult)
    assert result.candidate is None
    assert result.mismatch_reason == "ENTRY_GEOMETRY_MISMATCH"


def test_stop_mismatch_blocks_candidate() -> None:
    watch = _watch()
    admission = _admission_for(target=105.5, stop=98.5, entry=100.05)
    snap = admission.snapshot.model_copy(update={"stop_at_creation": 97.0})
    admission = admission.model_copy(update={"snapshot": snap})
    quote = _quote()
    geometry = executable_geometry_for_watch(watch, quote=quote, admission=admission)
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=geometry,
    )
    assert result.candidate is None
    assert result.mismatch_reason == "STOP_GEOMETRY_MISMATCH"


def test_target_mismatch_blocks_candidate() -> None:
    watch = _watch()
    admission = _admission_for(target=105.5, entry=100.05)
    snap = admission.snapshot.model_copy(update={"target_at_creation": 103.0})
    admission = admission.model_copy(update={"snapshot": snap})
    quote = _quote()
    geometry = executable_geometry_for_watch(
        watch,
        quote=quote,
        target_plan=TargetPlan(
            price=Decimal("105.5"),
            model="2R",
            reachability=TargetReachabilityClass.REALISTIC,
            two_r_target=Decimal(103),
        ),
        atr=1.0,
    )
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=geometry,
    )
    assert result.candidate is None
    assert result.mismatch_reason == "TARGET_GEOMETRY_MISMATCH"


def test_missing_entry_at_creation_blocks_modern_capital_path() -> None:
    watch = _watch()
    admission = _admission_for(target=105.5, include_entry_at_creation=False)
    quote = _quote()
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
    )
    assert result.candidate is None
    assert result.mismatch_reason == "ADMISSION_ENTRY_MISSING"


def test_build_candidate_matches_full_admission_geometry() -> None:
    watch = _watch(planned_target=Decimal(103))
    quote = _quote()
    recalc_target = 105.5
    admission = _admission_for(target=recalc_target, entry=100.05)
    snap = admission.snapshot.model_copy(update={"target_at_creation": recalc_target})
    admission = admission.model_copy(update={"snapshot": snap})
    geometry = executable_geometry_for_watch(
        watch,
        quote=quote,
        target_plan=TargetPlan(
            price=Decimal(str(recalc_target)),
            model="structure",
            reachability=TargetReachabilityClass.REALISTIC,
            two_r_target=Decimal(103),
        ),
        atr=1.0,
    )
    check = validate_geometry_against_admission_snapshot(
        geometry,
        admission.snapshot,
        exec_timeframe=watch.exec_timeframe,
        strategy_version=watch.strategy_version,
    )
    assert check.ok

    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=geometry,
    )
    assert result.candidate is not None
    built = result.candidate
    assert normalize_geometry_price(built.entry) == normalize_geometry_price(
        admission.snapshot.entry_at_creation
    )
    assert normalize_geometry_price(built.stop) == normalize_geometry_price(
        admission.snapshot.stop_at_creation
    )
    assert normalize_geometry_price(built.target) == normalize_geometry_price(
        admission.snapshot.target_at_creation
    )
    assert (
        geometry_hash_from_candidate(built, exec_timeframe=watch.exec_timeframe)
        == geometry.geometry_hash
    )


def test_geometry_built_inside_function_is_validated() -> None:
    watch = _watch()
    quote = _quote(ask="101.00")
    admission = _admission_for(target=105.5, entry=100.05)
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
    )
    assert result.candidate is None
    assert result.mismatch_reason == "ENTRY_GEOMETRY_MISMATCH"


def test_decimal_tick_normalization_does_not_false_reject() -> None:
    watch = _watch()
    admission = _admission_for(target=105.5, entry=100.0500)
    quote = _quote(ask="100.0500001", bid="100.00")
    geometry = executable_geometry_for_watch(watch, quote=quote, admission=admission)
    check = validate_geometry_against_admission_snapshot(
        geometry,
        admission.snapshot,
        exec_timeframe=watch.exec_timeframe,
        strategy_version=watch.strategy_version,
    )
    assert check.ok


def test_geometry_hash_mismatch_blocks_candidate() -> None:
    watch = _watch()
    admission = _admission_for(target=105.5, entry=100.05)
    quote = _quote()
    geometry = executable_geometry_for_watch(watch, quote=quote, admission=admission)
    wrong_hash_geometry = ExecutionGeometry(
        entry=geometry.entry,
        stop=geometry.stop,
        target=geometry.target,
        quote_bid=geometry.quote_bid,
        quote_ask=geometry.quote_ask,
        quote_ts=geometry.quote_ts,
        quote_source=geometry.quote_source,
        geometry_hash="deadbeef",
        effective_rr=geometry.effective_rr,
    )
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=wrong_hash_geometry,
    )
    assert result.candidate is None
    assert result.mismatch_reason == "GEOMETRY_HASH_MISMATCH"


def test_admission_record_hash_matches_candidate_when_target_recalculated() -> None:
    watch = _watch(planned_target=Decimal(103))
    quote = _quote()
    target_plan = TargetPlan(
        price=Decimal(106),
        model="structure",
        reachability=TargetReachabilityClass.REALISTIC,
        two_r_target=Decimal(103),
    )
    geometry = executable_geometry_for_watch(watch, quote=quote, target_plan=target_plan, atr=1.0)
    admission = _admission_for(target=float(target_plan.price), entry=float(geometry.entry))
    record_hash = geometry.geometry_hash
    result = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=geometry,
    )
    assert result.candidate is not None
    built = result.candidate
    assert record_hash == geometry_hash_from_candidate(built, exec_timeframe=watch.exec_timeframe)
    assert (
        compute_geometry_hash(
            entry=float(geometry.entry),
            stop=float(geometry.stop),
            target=float(admission.snapshot.target_at_creation),
            exec_timeframe=watch.exec_timeframe,
            strategy_version=watch.strategy_version,
        )
        == record_hash
    )
