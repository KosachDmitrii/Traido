"""One open EntryWatch per symbol — WAIT must not stack every scan."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from core.enums import EntryDecision, EntryWatchStatus, InstrumentThesis, TradeAction
from core.schemas import TradeCandidate
from database.session import init_db
from tests.unit.test_entry_timing_f3 import _snap
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.entry_watches import EntryWatchStore


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'wait_unique.db'}", future=True)
    init_db(eng)
    return eng


def _bundle_and_candidate(symbol: str, *, quality_score: int = 88):
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=quality_score)
    assert bundle.entry_decision is EntryDecision.WAIT_FOR_ENTRY
    cand = TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal("98.5"),
        target=Decimal(103),
        risk_reward=2.0,
        reasons=["x"],
        strategy_version="test@1",
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.WAIT_FOR_ENTRY,
        entry_quality=bundle.entry_quality,
        signal_price=Decimal(100),
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        pipeline_run_id=uuid4(),
    )
    return bundle, cand


def test_second_wait_for_same_symbol_refreshes_instead_of_stacking() -> None:
    store = EntryWatchStore()
    b1, c1 = _bundle_and_candidate("GLD", quality_score=80)
    b2, c2 = _bundle_and_candidate("GLD", quality_score=92)
    first = store.create_from_bundle(c1, b1)
    second = store.create_from_bundle(c2, b2)
    assert first.id == second.id
    assert second.entry_quality_at_creation == b2.entry_quality
    assert len(store.list_open()) == 1


def test_list_open_collapses_legacy_duplicates() -> None:
    store = EntryWatchStore()
    bundle, cand = _bundle_and_candidate("GLD")
    a = store.create_from_bundle(cand, bundle)
    twin = a.model_copy(update={"id": uuid4()})
    store._rows[twin.id] = twin
    assert len([w for w in store._rows.values() if w.status is EntryWatchStatus.WAITING]) == 2
    open_waits = store.list_open()
    assert len(open_waits) == 1
    invalidated = [w for w in store._rows.values() if w.status is EntryWatchStatus.INVALIDATED]
    assert len(invalidated) == 1
    assert "SUPERSEDED_SAME_SYMBOL" in invalidated[0].reasons


def test_ttl_expiry_persists_so_new_wait_does_not_integrity_error(engine) -> None:
    """Past-TTL WAIT expired only in memory used to leave SQLite waiting → UNIQUE fail."""
    from datetime import UTC, datetime, timedelta

    from trading.entry_watch_persistence import (
        configure_entry_watch_persistence,
        patch_entry_watch_store,
        persist_watch,
    )
    from trading.entry_watches import EntryWatchStore, WAIT_EXPIRED

    configure_entry_watch_persistence(enabled=True)
    try:
        store = EntryWatchStore()
        patch_entry_watch_store(store, engine=engine)
        b1, c1 = _bundle_and_candidate("NFLX")
        first = store.create_from_bundle(c1, b1)
        # Simulate the old bug: memory expired, DB still waiting.
        stale = first.model_copy(
            update={
                "status": EntryWatchStatus.EXPIRED,
                "valid_until": datetime.now(UTC) - timedelta(minutes=5),
                "reasons": [*first.reasons, WAIT_EXPIRED],
            }
        )
        store._rows[first.id] = stale
        # DB still has the active waiting row (bypass patched update).
        persist_watch(
            first.model_copy(
                update={"valid_until": datetime.now(UTC) - timedelta(minutes=5)}
            ),
            engine=engine,
        )

        b2, c2 = _bundle_and_candidate("NFLX", quality_score=90)
        second = store.create_from_bundle(c2, b2)
        assert second.status is EntryWatchStatus.WAITING
        assert second.id != first.id
    finally:
        configure_entry_watch_persistence(enabled=False)


def test_list_actionable_persists_ttl_expiry(engine) -> None:
    from datetime import UTC, datetime, timedelta

    from database.models.desk import EntryWatchRow
    from database.session import session_factory
    from trading.entry_watch_persistence import (
        configure_entry_watch_persistence,
        patch_entry_watch_store,
    )
    from trading.entry_watches import EntryWatchStore, WAIT_EXPIRED

    configure_entry_watch_persistence(enabled=True)
    try:
        store = EntryWatchStore()
        patch_entry_watch_store(store, engine=engine)
        bundle, cand = _bundle_and_candidate("MRK")
        watch = store.create_from_bundle(cand, bundle)
        store._rows[watch.id] = watch.model_copy(
            update={"valid_until": datetime.now(UTC) - timedelta(seconds=1)}
        )
        assert store.list_actionable() == []
        assert store.get(watch.id).status is EntryWatchStatus.EXPIRED
        assert WAIT_EXPIRED in store.get(watch.id).reasons

        SessionLocal = session_factory(engine)
        with SessionLocal() as session:
            row = session.get(EntryWatchRow, watch.id)
            assert row is not None
            assert row.status == EntryWatchStatus.EXPIRED.value
    finally:
        configure_entry_watch_persistence(enabled=False)


def test_price_in_zone_uses_admission_atr_cushion() -> None:
    """Trigger band matches TradeAdmission ±0.2 ATR — no BUY invent, no edge flap."""
    from datetime import UTC, datetime, timedelta

    from core.enums import InstrumentThesis, SetupType
    from core.schemas import AdmissionSnapshot, EntryWatch
    from trading.entry_watches import price_in_zone

    now = datetime.now(UTC)
    watch = EntryWatch(
        id=uuid4(),
        symbol="TEST",
        strategy_version="t@1",
        created_at=now,
        valid_until=now + timedelta(hours=1),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(100),
        current_price_at_creation=Decimal(100),
        entry_zone_low=Decimal(98),
        entry_zone_high=Decimal(100),
        planned_entry=Decimal(99),
        planned_stop=Decimal(97),
        planned_target=Decimal(103),
        entry_quality_at_creation=70,
        status=EntryWatchStatus.WAITING,
        admission_snapshot=AdmissionSnapshot(
            price_at_creation=100.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=98.0,
            entry_zone_high=100.0,
        ),
    )
    assert price_in_zone(100.35, watch) is True
    assert price_in_zone(100.45, watch) is False
    assert price_in_zone(97.65, watch) is True
    assert price_in_zone(97.55, watch) is False
