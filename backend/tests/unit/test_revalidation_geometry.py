"""Watch revalidation must publish the same geometry admission checked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import (
    AdmissionDecision,
    EntryDecision,
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
from trading.entry_watch_eval import build_candidate_from_revalidation
from trading.execution_geometry import (
    executable_geometry_for_watch,
    geometry_matches_admission_snapshot,
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
    *, target: float, stop: float = 98.5, entry: float = 100.05
) -> TradeAdmissionResult:
    snap = AdmissionSnapshot(
        price_at_creation=entry,
        atr_at_creation=1.0,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=70,
        entry_quality_at_creation=70,
        stop_at_creation=stop,
        target_at_creation=target,
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


def test_build_candidate_matches_admission_target_and_hash() -> None:
    watch = _watch(planned_target=Decimal(103))
    quote = _quote()
    recalc_target = 105.5
    admission = _admission_for(target=recalc_target)
    geometry = executable_geometry_for_watch(
        watch, quote=quote, target_plan=None, admission=admission
    )
    assert geometry_matches_admission_snapshot(geometry, admission.snapshot)

    built = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=geometry,
    )
    assert built is not None
    assert float(built.target) == recalc_target
    assert float(built.target) != float(watch.planned_target)
    assert (
        geometry_hash_from_candidate(built, exec_timeframe=watch.exec_timeframe)
        == geometry.geometry_hash
    )
    assert float(built.admission_snapshot["target_at_creation"]) == recalc_target


def test_geometry_mismatch_returns_no_candidate() -> None:
    watch = _watch()
    quote = _quote()
    admission = _admission_for(target=105.5)
    wrong_geometry = executable_geometry_for_watch(
        watch,
        quote=quote,
        target_plan=TargetPlan(
            price=Decimal(103),
            model="2R",
            reachability=TargetReachabilityClass.REALISTIC,
            two_r_target=Decimal(103),
        ),
        atr=1.0,
    )
    assert (
        build_candidate_from_revalidation(
            watch,
            base=_base_candidate(watch),
            admission=admission,
            quote=quote,
            geometry=wrong_geometry,
        )
        is None
    )


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
    admission = _admission_for(target=float(target_plan.price))
    record_hash = geometry.geometry_hash
    built = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
        geometry=geometry,
    )
    assert built is not None
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


def test_build_candidate_without_geometry_uses_admission_snapshot_target() -> None:
    watch = _watch(planned_target=Decimal(103))
    quote = _quote()
    admission = _admission_for(target=107.25)
    built = build_candidate_from_revalidation(
        watch,
        base=_base_candidate(watch),
        admission=admission,
        quote=quote,
    )
    assert built is not None
    assert float(built.target) == 107.25
    assert built.entry_decision is EntryDecision.BUY_NOW
