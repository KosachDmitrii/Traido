"""Zone-aligned WAIT levels and stale invalidation."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from core.enums import EntryDecision, InstrumentThesis, TradeAction
from core.schemas import TradeCandidate
from tests.unit.test_entry_timing_f3 import _snap
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.entry_watches import EntryWatch, EntryWatchStatus
from trading.wait_plan import derive_wait_levels, stale_invalidate_reason


def _extended_facts():
    return evaluate_timing(
        _snap(close=51.78, sma20=49.98, atr=1.17, vwap=45.0, resistance=[55.0]),
        signal_price=51.78,
        planned_entry=49.98,
        planned_stop=48.2,
        planned_target=52.0,
    )


def test_wait_levels_anchor_to_zone_not_sma() -> None:
    facts = _extended_facts()
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=70)
    # Soft VWAP extension is a confirmation concern, not a candidate veto.
    # Force WAIT so this test can assert zone-anchored wait geometry.
    bundle = bundle.model_copy(update={"entry_decision": EntryDecision.WAIT_FOR_ENTRY})
    assert bundle.entry_decision is EntryDecision.WAIT_FOR_ENTRY
    cand = TradeCandidate(
        symbol="CNQ",
        action=TradeAction.BUY,
        confidence=0.7,
        entry=Decimal("49.98"),
        stop=Decimal("48.2"),
        target=Decimal("52.0"),
        risk_reward=1.5,
        reasons=["x"],
        strategy_version="test@1",
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.WAIT_FOR_ENTRY,
        entry_quality=bundle.entry_quality,
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        pipeline_run_id=uuid4(),
    )
    plan = derive_wait_levels(bundle, cand)
    assert float(plan.entry) == float(bundle.entry_zone_high)
    assert float(plan.stop) < float(bundle.entry_zone_low)
    assert float(plan.target) > float(plan.entry)


def test_stale_when_price_passed_target() -> None:
    watch = EntryWatch(
        id=uuid4(),
        symbol="CNQ",
        strategy_version="t@1",
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        valid_until=__import__("datetime").datetime.now(__import__("datetime").UTC),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal("51.78"),
        current_price_at_creation=Decimal("51.78"),
        entry_zone_low=Decimal("44.25"),
        entry_zone_high=Decimal("46.15"),
        planned_entry=Decimal("46.15"),
        planned_stop=Decimal("43.5"),
        planned_target=Decimal("48.5"),
        entry_quality_at_creation=41,
        status=EntryWatchStatus.WAITING,
    )
    assert stale_invalidate_reason(watch, 51.78) == "REWARD_RISK_DROPPED"
