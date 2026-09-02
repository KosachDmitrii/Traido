"""P0 fail-closed ApprovalAdmission authority — LLY / NEM / missing data.

Every negative path asserts broker.place_order call_count == 0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    AdmissionDecision,
    EntryDecision,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
    TradeAction,
    TradingMode,
    UserDecision,
)
from core.schemas import (
    EntryDecisionBundle,
    EntryQualityBreakdown,
    EntryTimingFacts,
    Quote,
    TargetPlan,
    TradeCandidate,
)
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import (
    CLEARED_EARNINGS,
    admission_ready_candidate,
    ensure_admission_ready,
    liquid_market_data,
)
from trading.execution import ExecutionService
from trading import external_positions as external_positions_mod
from trading.exits import MemoryExitStore
from trading.final_pretrade import map_admission_rejection
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore
from trading.trade_admission import evaluate_trade_admission


@pytest.fixture(autouse=True)
def _kill_off() -> None:
    set_kill_switch(False)


@pytest.mark.asyncio
async def test_normalized_lly_unrealistic_target_blocks_with_zero_broker_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High scores + Risk PASS cannot overcome TARGET_UNREALISTIC."""
    now = datetime(2024, 6, 3, 15, 30, tzinfo=UTC)
    monkeypatch.setattr("trading.execution._utcnow", lambda: now)

    cand = admission_ready_candidate(
        symbol="LLY",
        entry=800.0,
        stop=780.0,
        target=860.0,
        target_reachability=TargetReachabilityClass.UNREALISTIC,
        target_model="structure",
    )
    cand = cand.model_copy(update={"setup_quality": 95, "entry_quality": 95, "confidence": 0.99})

    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place

    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    risk = RiskEngine().evaluate(cand, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(cand, risk, TradingMode.CONFIRMATION)

    facts = EntryTimingFacts(
        current_price=800.0,
        atr=5.0,
        nearest_support=780.0,
        stop_distance_atr=4.0,
        stop_distance_pct=2.5,
    )
    plan = TargetPlan(
        price=cand.target,
        model="structure",
        reachability=TargetReachabilityClass.UNREALISTIC,
    )
    bundle = EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=95,
        setup_quality=95,
        breakdown=EntryQualityBreakdown(
            price_location=90,
            vwap_location=90,
            atr_extension=90,
            pullback_quality=90,
            remaining_reward=90,
            support_structure=90,
            resistance_structure=90,
            short_term_momentum=90,
            volume_confirmation=90,
            market_alignment=90,
            signal_drift=90,
        ),
        facts=facts,
        entry_zone_low=cand.entry_zone_low,
        entry_zone_high=cand.entry_zone_high,
        stop_price=cand.stop,
        target=plan,
    )
    admission = evaluate_trade_admission(
        bundle=bundle,
        candidate=cand,
        quote=Quote(
            symbol="LLY",
            bid=Decimal("799.9"),
            ask=Decimal("800.1"),
            ts=now,
            source="test",
        ),
        bars_count=60,
        last_bar_ts=now - timedelta(minutes=30),
        require_bars=True,
        entry=cand.entry,
        stop=cand.stop,
        target=cand.target,
        target_plan=plan,
        now=now,
    )
    assert admission.decision is AdmissionDecision.NO_TRADE
    assert "TARGET_UNREALISTIC" in admission.vetoes or any(
        "TARGET_UNREALISTIC" in r for r in admission.reason_codes
    )
    assert map_admission_rejection(admission) == "NO_TRADE/TARGET_UNREALISTIC"

    svc = ExecutionService(
        broker=broker,
        store=store,
        risk_engine=RiskEngine(),
        intents=intents,
        market_data=liquid_market_data(price=800.0),
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )
    with pytest.raises(RuntimeError) as e:
        await svc.decide(opp.id, UserDecision.APPROVE)
    assert "TARGET_UNREALISTIC" in str(e.value) or "NO_TRADE" in str(e.value)
    assert place.call_count == 0
    assert intents.list_by_key_prefix(f"entry:{opp.id}:") == []


@pytest.mark.asyncio
async def test_raw_legacy_lly_requires_admission_zero_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical card without admission metadata → fail-closed, no broker."""
    now = datetime(2024, 6, 3, 15, 30, tzinfo=UTC)
    monkeypatch.setattr("trading.execution._utcnow", lambda: now)
    monkeypatch.setattr("tests.support.ensure_admission_ready", lambda c: c)

    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place

    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    legacy = TradeCandidate(
        symbol="LLY",
        action=TradeAction.BUY,
        confidence=0.95,
        entry=Decimal("800"),
        stop=Decimal("780"),
        target=Decimal("860"),
        risk_reward=3.0,
        reasons=["legacy card"],
        strategy_version="legacy@0",
        thesis=InstrumentThesis.BULLISH,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=90,
        entry_quality=88,
    )
    risk = RiskEngine().evaluate(legacy, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(legacy, risk, TradingMode.CONFIRMATION)

    svc = ExecutionService(
        broker=broker,
        store=store,
        risk_engine=RiskEngine(),
        intents=intents,
        market_data=liquid_market_data(price=800.0),
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )
    with pytest.raises(RuntimeError) as e:
        await svc.decide(opp.id, UserDecision.APPROVE)
    detail = str(e.value)
    assert place.call_count == 0
    assert intents.list_by_key_prefix(f"entry:{opp.id}:") == []
    assert any(
        token in detail
        for token in (
            "ADMISSION_REQUIRED",
            "BUY_REJECTED",
            "MISSING_ENTRY_ZONE",
            "PRICE_OUTSIDE",
            "TARGET_PLAN_REQUIRED",
        )
    )


@pytest.mark.asyncio
async def test_nem_orphan_creates_external_incident_not_admission() -> None:
    from trading.reconcile import block_symbol_as_unknown

    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()

    incident = await block_symbol_as_unknown(
        intents,
        symbol="NEM",
        qty=Decimal("100"),
        reason="broker position with no ledger row",
        audit=audit,
        avg_entry=Decimal("50"),
        broker="ibkr",
    )
    assert incident.symbol == "NEM"
    assert incident.correlation_status == "unattributed"
    assert external_positions_mod.EXTERNAL_POSITIONS.get_open_for_symbol("NEM") is not None
    assert intents.list_by_key_prefix("entry:") == []
    assert intents.list_by_key_prefix("orphan:NEM:") == []


@pytest.mark.asyncio
async def test_aged_lly_card_invalidated_after_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading import opportunities as opp_mod
    from trading.reconcile import block_symbol_as_unknown

    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    monkeypatch.setattr(opp_mod, "OPPORTUNITIES", store)

    cand = admission_ready_candidate(symbol="LLY", entry=800.0, stop=780.0, target=860.0)
    risk = RiskEngine().evaluate(cand, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(cand, risk, TradingMode.CONFIRMATION)
    assert store.get(opp.id) is not None

    intents = MemoryOrderIntentStore()
    await block_symbol_as_unknown(
        intents, symbol="LLY", qty=Decimal("10"), reason="orphan after flatten"
    )
    updated = store.get(opp.id)
    assert updated is None or updated.status.value != "awaiting_confirmation"


def test_missing_stop_blocks() -> None:
    now = datetime.now(UTC)
    facts = EntryTimingFacts(current_price=100.0, atr=2.0, nearest_support=95.0)
    plan = TargetPlan(
        price=Decimal("115"), model="structure", reachability=TargetReachabilityClass.REALISTIC
    )
    bundle = EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=80,
        setup_quality=80,
        breakdown=EntryQualityBreakdown(
            price_location=70,
            vwap_location=70,
            atr_extension=70,
            pullback_quality=70,
            remaining_reward=70,
            support_structure=70,
            resistance_structure=70,
            short_term_momentum=70,
            volume_confirmation=70,
            market_alignment=70,
            signal_drift=70,
        ),
        facts=facts,
        target=plan,
        stop_price=None,
        entry_zone_low=Decimal("99.5"),
        entry_zone_high=Decimal("100.5"),
    )
    result = evaluate_trade_admission(
        bundle=bundle,
        candidate=None,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=Quote(
            symbol="AAPL",
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            ts=now,
            source="test",
        ),
        bars_count=60,
        last_bar_ts=now - timedelta(minutes=20),
        require_bars=True,
        entry=Decimal("100"),
        stop=None,
        target=Decimal("115"),
        target_plan=plan,
        now=now,
    )
    assert result.admitted is False
    assert result.decision is not AdmissionDecision.BUY_ALLOWED
    assert "MISSING_STOP" in result.vetoes or "MISSING_STOP" in result.reason_codes


def test_entry_intent_without_admission_fk_refused() -> None:
    from core.enums import IntentPurpose, IntentStatus, OrderSide, OrderType
    from trading.order_intent import OrderIntent

    intents = MemoryOrderIntentStore()
    with pytest.raises(ValueError, match="entry_intent_requires_approval_admission"):
        intents.create_or_get(
            OrderIntent(
                idempotency_key="entry:test:0",
                purpose=IntentPurpose.ENTRY,
                broker="paper",
                symbol="AAPL",
                side=OrderSide.BUY,
                requested_qty=Decimal("1"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("100"),
                status=IntentStatus.CREATED,
                approval_admission_record_id=None,
            )
        )


def test_ensure_admission_ready_does_not_lift_target() -> None:
    cand = admission_ready_candidate(entry=100.0, stop=95.0, target=105.0)
    out = ensure_admission_ready(cand)
    assert float(out.target) == 105.0
