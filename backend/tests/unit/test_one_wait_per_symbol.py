"""One open EntryWatch per symbol — WAIT must not stack every scan."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from core.enums import EntryDecision, EntryWatchStatus, InstrumentThesis, TradeAction
from core.schemas import TradeCandidate
from tests.unit.test_entry_timing_f3 import _snap
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.entry_watches import EntryWatchStore


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


def test_different_symbols_still_get_separate_watches() -> None:
    store = EntryWatchStore()
    b1, c1 = _bundle_and_candidate("GLD")
    b2, c2 = _bundle_and_candidate("SLV")
    gld = store.create_from_bundle(c1, b1)
    slv = store.create_from_bundle(c2, b2)
    assert gld.id != slv.id
    assert {w.symbol for w in store.list_open()} == {"GLD", "SLV"}
