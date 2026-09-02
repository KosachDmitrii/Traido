"""Production hardening Stage 2–5 regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, InstrumentThesis, SetupType, Timeframe
from core.schemas import Bar, EntryWatch
from trading.entry_watch_transitions import enrich_new_watch_fields, try_transition
from trading.entry_watches import ENTRY_WATCHES
from trading.geometry_hash import compute_geometry_hash, geometry_hash_from_watch
from trading.zone_arrival import evaluate_zone_arrival


def _watch(**kwargs) -> EntryWatch:
    now = datetime.now(UTC)
    base = EntryWatch(
        id=uuid4(),
        symbol="NEM",
        strategy_version="test@1",
        created_at=now,
        valid_until=now,
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(123),
        current_price_at_creation=Decimal(123),
        entry_zone_low=Decimal("111.8"),
        entry_zone_high=Decimal("113.2"),
        planned_entry=Decimal("112.5"),
        planned_stop=Decimal(110),
        planned_target=Decimal(117),
        entry_quality_at_creation=70,
        setup_type=SetupType.PULLBACK_CONTINUATION,
    )
    w = enrich_new_watch_fields(base)
    return w.model_copy(update=kwargs)


def test_geometry_hash_stable():
    h1 = compute_geometry_hash(
        entry=112.5, stop=110, target=117, exec_timeframe="H1", strategy_version="test@1"
    )
    h2 = compute_geometry_hash(
        entry=112.5, stop=110, target=117, exec_timeframe="H1", strategy_version="test@1"
    )
    assert h1 == h2
    assert len(h1) == 16


def test_geometry_hash_changes_with_target():
    w = _watch()
    h1 = geometry_hash_from_watch(w)
    w2 = w.model_copy(update={"planned_target": Decimal(118)})
    assert geometry_hash_from_watch(w2) != h1


def test_cas_transition_in_memory():
    ENTRY_WATCHES.clear()
    w = _watch()
    ENTRY_WATCHES.update(w)
    out = ENTRY_WATCHES.mark(w.id, EntryWatchStatus.TRIGGERED, reason="TEST")
    assert out is not None
    assert out.status is EntryWatchStatus.TRIGGERED
    assert out.trigger_version == 1


def test_invalid_transition_denied():
    w = _watch(status=EntryWatchStatus.WAITING)
    assert try_transition(w, EntryWatchStatus.CONVERTED, reason="BAD") is None


def test_gap_uses_previous_close_not_candle_body():
    """FR-014: gap = (open - prev_close) / prev_close, not body of one bar."""
    now = datetime.now(UTC)
    watch = _watch()
    bars = []
    for i in range(10):
        bars.append(
            Bar(
                symbol="NEM",
                timeframe=Timeframe.M5,
                ts=now,
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1_000_000,
                source="test",
            )
        )
    # Large gap down: prev close 100, next open 97
    bars[-1] = Bar(
        symbol="NEM",
        timeframe=Timeframe.M5,
        ts=now,
        open=97.0,
        high=97.5,
        low=96.5,
        close=96.8,
        volume=2_000_000,
        source="test",
    )
    bars[-2] = Bar(
        symbol="NEM",
        timeframe=Timeframe.M5,
        ts=now,
        open=100.0,
        high=100.2,
        low=99.8,
        close=100.0,
        volume=1_000_000,
        source="test",
    )
    facts = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=112.0)
    assert facts.gap_down_pct is not None
    assert facts.gap_down_pct >= 2.9


def test_admission_evaluation_key_idempotent(isolated_admission_and_watches):
    from core.enums import AdmissionDecision, DataHealthStatus
    from core.schemas import TradeAdmissionResult
    from trading.admission_records import ADMISSION_RECORDS

    entity = uuid4()
    gh = "abc123def456"
    adm = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        data_status=DataHealthStatus.HEALTHY,
    )
    r1 = ADMISSION_RECORDS.record(
        symbol="AAPL",
        admission=adm,
        pipeline_run_id=entity,
        geometry_hash=gh,
        phase="creation",
    )
    r2 = ADMISSION_RECORDS.record(
        symbol="AAPL",
        admission=adm,
        pipeline_run_id=entity,
        geometry_hash=gh,
        phase="creation",
    )
    assert r1.id == r2.id
