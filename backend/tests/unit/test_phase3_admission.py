"""Phase 3 — admission audit, explain, shadow outcomes, watch persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from core.enums import (
    AdmissionDecision,
    EntryDecision,
    EntryWatchStatus,
    InstrumentThesis,
    SetupType,
)
from core.schemas import AdmissionSnapshot, EntryWatch, TradeAdmissionResult
from database.session import init_db
from trading.admission_records import AdmissionRecordStore
from trading.entry_watch_persistence import (
    configure_entry_watch_persistence,
    hydrate_entry_watches,
    patch_entry_watch_store,
)
from trading.entry_watches import EntryWatchStore
from trading.explain_trade_admission import explain_from_admission, explain_trade_admission
from trading.shadow_outcomes import ShadowOutcomeStore
from trading.trade_admission import ADMISSION_VERSION
from trading.zone_touch_calibration import lookup_zone_touch_calibration


def _admission(*, decision: AdmissionDecision = AdmissionDecision.BUY_ALLOWED) -> TradeAdmissionResult:
    return TradeAdmissionResult(
        decision=decision,
        admitted=decision is AdmissionDecision.BUY_ALLOWED,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=84,
        entry_quality=78,
        effective_rr=2.4,
        chase_score=14,
        structure_valid=True,
        stop_valid=True,
        target_valid=True,
        reason_codes=["BUY_ALLOWED"] if decision is AdmissionDecision.BUY_ALLOWED else ["WAIT"],
        admission_version=ADMISSION_VERSION,
    )


def _watch() -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="NEM",
        strategy_version="test",
        created_at=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=2),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(123),
        current_price_at_creation=Decimal(123),
        entry_zone_low=Decimal("111.8"),
        entry_zone_high=Decimal("113.2"),
        planned_entry=Decimal("112.5"),
        planned_stop=Decimal(108),
        planned_target=Decimal(125),
        entry_quality_at_creation=70,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=84,
        status=EntryWatchStatus.WAITING,
        admission_snapshot=AdmissionSnapshot(
            price_at_creation=123.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=111.8,
            entry_zone_high=113.2,
        ),
    )


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'phase3.db'}", future=True)
    init_db(eng)
    return eng


def test_admission_record_persist_and_explain(engine) -> None:
    store = AdmissionRecordStore(engine=engine)
    watch_id = uuid4()
    admission = _admission()
    rec = store.record(
        symbol="NEM",
        admission=admission,
        watch_id=watch_id,
        zone_arrival_quality=81,
        zone_arrival_type="HEALTHY_PULLBACK",
    )
    loaded = store.latest_for_watch(watch_id)
    assert loaded is not None
    assert loaded.decision is AdmissionDecision.BUY_ALLOWED
    assert loaded.zone_arrival_quality == 81

    explain = explain_from_admission(
        symbol="NEM",
        admission=rec,
        entity_type="watch",
        entity_id=str(watch_id),
    )
    assert explain.headline == "WHY WAS THIS TRADE ALLOWED?"
    assert explain.admitted is True
    labels = {f.label: f.value for f in explain.fields}
    assert labels["Setup quality"] == "84"
    assert labels["Arrival"] == "81 (HEALTHY_PULLBACK)"
    assert labels["Effective R:R"] == "2.40"
    assert labels["Vetoes"] == "none"

    import trading.admission_records as admission_mod

    admission_mod.ADMISSION_RECORDS = store
    via_api = explain_trade_admission(admission_record_id=rec.id)
    assert via_api is not None
    assert via_api.decision is AdmissionDecision.BUY_ALLOWED


def test_explain_wait_decision(engine) -> None:
    store = AdmissionRecordStore(engine=engine)
    admission = _admission(decision=AdmissionDecision.WAIT)
    admission = admission.model_copy(
        update={
            "admitted": False,
            "vetoes": ["ENTRY_OUTSIDE_ALLOWED_ZONE"],
            "reason_codes": ["ENTRY_OUTSIDE_ALLOWED_ZONE"],
        }
    )
    rec = store.record(symbol="NEM", admission=admission)
    explain = explain_from_admission(
        symbol="NEM",
        admission=rec,
        entity_type="admission_record",
        entity_id=str(rec.id),
    )
    assert "WAIT" in explain.headline or "WAITING" in explain.headline
    assert "ENTRY_OUTSIDE_ALLOWED_ZONE" in explain.vetoes


def test_shadow_outcome_tracks_zone_and_mfe(engine) -> None:
    shadows = ShadowOutcomeStore(engine=engine)
    watch = _watch()
    rec = shadows.begin_from_watch(
        watch,
        origin="pipeline",
        entry_decision=EntryDecision.WAIT_FOR_ENTRY,
        reference_price=120.0,
        distance_atr=3.5,
    )
    assert rec.zone_reached is False

    shadows.update_price("NEM", 112.0)
    shadows.update_price("NEM", 113.0)

    rows = shadows.list_completed()
    assert rows == []

    # Force complete by backdating shadow_until via re-read active row update
    from sqlalchemy.orm import sessionmaker

    from database.models.desk import ShadowOutcomeRow

    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        row = session.get(ShadowOutcomeRow, rec.id)
        assert row is not None
        payload = dict(row.payload)
        payload["shadow_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        row.payload = payload
        row.shadow_until = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

    shadows.finalize_expired()
    done = shadows.list_completed()
    assert len(done) == 1
    assert done[0].zone_reached is True
    assert done[0].mfe_pct is not None
    assert done[0].time_to_zone_minutes is not None


def test_calibration_requires_minimum_sample(engine) -> None:
    cal = lookup_zone_touch_calibration(
        setup_type=SetupType.PULLBACK_CONTINUATION,
        distance_atr=2.5,
    )
    assert cal.calibrated is False
    assert cal.sample_size < 100


def test_watch_persistence_roundtrip(engine) -> None:
    configure_entry_watch_persistence(enabled=True)
    try:
        store = EntryWatchStore()
        patch_entry_watch_store(store, engine=engine)
        watch = _watch()
        store.update(watch)

        other = EntryWatchStore()
        n = hydrate_entry_watches(other, engine=engine)
        assert n == 1
        loaded = other.get(watch.id)
        assert loaded is not None
        assert loaded.symbol == "NEM"
    finally:
        configure_entry_watch_persistence(enabled=False)
