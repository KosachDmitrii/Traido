"""Immutable executable geometry — one authority for admission, evidence, intent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.schemas import AdmissionSnapshot, EntryWatch, Quote, TargetPlan, TradeAdmissionResult
from trading.effective_rr import compute_effective_rr
from trading.geometry_hash import compute_geometry_hash


@dataclass(frozen=True)
class ExecutionGeometry:
    entry: Decimal
    stop: Decimal
    target: Decimal
    quote_bid: Decimal
    quote_ask: Decimal
    quote_ts: datetime
    quote_source: str | None
    geometry_hash: str
    effective_rr: float | None


@dataclass(frozen=True)
class GeometryValidationResult:
    ok: bool
    reason: str | None = None


def normalize_geometry_price(value: Decimal | float | str) -> float:
    """Same rounding as ``compute_geometry_hash`` — single canonical price form."""
    return round(float(value), 4)


def resolve_capital_atr(
    *,
    facts_atr: float | None = None,
    snapshot_atr: float | None = None,
    indicator_atr: float | None = None,
) -> float | None:
    """Return real ATR only — never synthesize for capital path."""
    for candidate in (facts_atr, snapshot_atr, indicator_atr):
        if isinstance(candidate, (int, float)) and candidate > 0:
            return float(candidate)
    return None


def build_execution_geometry(
    *,
    entry: Decimal | float,
    stop: Decimal | float,
    target: Decimal | float,
    quote: Quote,
    exec_timeframe: str,
    strategy_version: str,
    zone_low: float | None = None,
    zone_high: float | None = None,
    atr: float | None = None,
) -> ExecutionGeometry:
    """Build one geometry bundle used across admission, evidence, and intent."""
    ent = Decimal(str(entry))
    stp = Decimal(str(stop))
    tgt = Decimal(str(target))
    bid = quote.bid if quote.bid is not None else Decimal(0)
    ask = quote.ask if quote.ask is not None else ent
    rr = compute_effective_rr(
        entry=ent,
        stop=stp,
        target=tgt,
        quote=quote,
        zone_low=zone_low,
        zone_high=zone_high,
        atr=atr,
    )
    gh = compute_geometry_hash(
        entry=float(ent),
        stop=float(stp),
        target=float(tgt),
        exec_timeframe=exec_timeframe,
        strategy_version=strategy_version,
    )
    return ExecutionGeometry(
        entry=ent,
        stop=stp,
        target=tgt,
        quote_bid=bid,
        quote_ask=ask,
        quote_ts=quote.ts,
        quote_source=getattr(quote, "source", None),
        geometry_hash=gh,
        effective_rr=rr.effective_rr,
    )


def _resolve_executable_target(
    watch: EntryWatch,
    *,
    target_plan: TargetPlan | None = None,
    admission: TradeAdmissionResult | None = None,
) -> Decimal:
    """Executable target price — never fall back to stale watch.planned_target when fresher data exists."""
    if target_plan is not None:
        return target_plan.price
    snap = admission.snapshot if admission is not None else None
    if snap is not None and snap.target_at_creation is not None:
        return Decimal(str(snap.target_at_creation))
    return watch.planned_target


def executable_geometry_for_watch(
    watch: EntryWatch,
    *,
    quote: Quote,
    target_plan: TargetPlan | None = None,
    admission: TradeAdmissionResult | None = None,
    atr: float | None = None,
) -> ExecutionGeometry:
    """One geometry bundle for watch revalidation → candidate → admission record."""
    zone_low = float(watch.entry_zone_low) if watch.entry_zone_low else None
    zone_high = float(watch.entry_zone_high) if watch.entry_zone_high else None
    resolved_atr = resolve_capital_atr(
        facts_atr=atr,
        snapshot_atr=(
            admission.snapshot.atr_at_creation
            if admission is not None and admission.snapshot is not None
            else None
        ),
        indicator_atr=(
            watch.admission_snapshot.atr_at_creation if watch.admission_snapshot else None
        ),
    )
    return build_execution_geometry(
        entry=quote.ask or watch.planned_entry,
        stop=watch.planned_stop,
        target=_resolve_executable_target(watch, target_plan=target_plan, admission=admission),
        quote=quote,
        exec_timeframe=watch.exec_timeframe,
        strategy_version=watch.strategy_version,
        zone_low=zone_low,
        zone_high=zone_high,
        atr=resolved_atr,
    )


def validate_geometry_against_admission_snapshot(
    geometry: ExecutionGeometry,
    snapshot: AdmissionSnapshot,
    *,
    exec_timeframe: str,
    strategy_version: str,
) -> GeometryValidationResult:
    """Full executable geometry must match the snapshot that received BUY_ALLOWED."""
    if snapshot.entry_at_creation is None:
        return GeometryValidationResult(ok=False, reason="ADMISSION_ENTRY_MISSING")
    if snapshot.stop_at_creation is None or snapshot.target_at_creation is None:
        return GeometryValidationResult(ok=False, reason="REVALIDATION_GEOMETRY_MISMATCH")

    if normalize_geometry_price(geometry.entry) != normalize_geometry_price(
        snapshot.entry_at_creation
    ):
        return GeometryValidationResult(ok=False, reason="ENTRY_GEOMETRY_MISMATCH")
    if normalize_geometry_price(geometry.stop) != normalize_geometry_price(
        snapshot.stop_at_creation
    ):
        return GeometryValidationResult(ok=False, reason="STOP_GEOMETRY_MISMATCH")
    if normalize_geometry_price(geometry.target) != normalize_geometry_price(
        snapshot.target_at_creation
    ):
        return GeometryValidationResult(ok=False, reason="TARGET_GEOMETRY_MISMATCH")

    expected_hash = compute_geometry_hash(
        entry=snapshot.entry_at_creation,
        stop=snapshot.stop_at_creation,
        target=snapshot.target_at_creation,
        exec_timeframe=exec_timeframe,
        strategy_version=strategy_version,
    )
    if geometry.geometry_hash != expected_hash:
        return GeometryValidationResult(ok=False, reason="GEOMETRY_HASH_MISMATCH")
    return GeometryValidationResult(ok=True)


def geometry_matches_admission_snapshot(
    geometry: ExecutionGeometry,
    snapshot: AdmissionSnapshot,
    *,
    exec_timeframe: str = "H1",
    strategy_version: str = "",
) -> bool:
    """Backward-compatible bool wrapper — prefer ``validate_geometry_against_admission_snapshot``."""
    return validate_geometry_against_admission_snapshot(
        geometry,
        snapshot,
        exec_timeframe=exec_timeframe,
        strategy_version=strategy_version,
    ).ok
