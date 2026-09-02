"""Crash injection + sealed ApprovalEvidence + concurrent distinct request_ids."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from broker.paper.mock import MockPaperBroker
from core.enums import (
    AdmissionDecision,
    EntryDecision,
    InstrumentThesis,
    OrderSide,
    SetupType,
    TargetReachabilityClass,
    UserDecision,
)
from core.schemas import (
    AdmissionInput,
    ApprovalCommand,
    EntryDecisionBundle,
    EntryQualityBreakdown,
    EntryTimingFacts,
    Quote,
    TargetPlan,
    TradeAdmissionResult,
)
from database.session import init_db
from tests.unit.test_strict_admission_authority import _approved_opp, _service
from trading.admission_records import AdmissionRecordStore, build_request_fingerprint
from trading.approval_commit import commit_approval_bundle
from trading.approval_errors import ApprovalDomainError, EntryInFlightError
from trading.approval_evidence import evaluate_final_approval
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore

pytestmark = pytest.mark.usefixtures("capital_path_ready")


def _breakdown() -> EntryQualityBreakdown:
    return EntryQualityBreakdown(
        price_location=80,
        vwap_location=80,
        atr_extension=80,
        pullback_quality=80,
        remaining_reward=80,
        support_structure=80,
        resistance_structure=80,
        short_term_momentum=80,
        volume_confirmation=80,
        market_alignment=80,
        signal_drift=80,
    )


def _input(**overrides) -> AdmissionInput:
    from core.enums import EarningsCheck, NewsCheck
    from core.schemas import MarketAssessment, StopPlan

    facts = EntryTimingFacts(current_price=100.0, atr=2.0, stop_distance_atr=2.0)
    bundle = EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=80,
        setup_quality=80,
        breakdown=_breakdown(),
        facts=facts,
        chase_reasons=[],
        reasons=["ok"],
        entry_zone_low=Decimal("99"),
        entry_zone_high=Decimal("101"),
        stop_price=Decimal("95"),
        target=TargetPlan(
            price=Decimal(110),
            model="test",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
    )
    base = {
        "bundle": bundle,
        "setup_type": SetupType.PULLBACK_CONTINUATION,
        "setup_quality": 80,
        "entry_zone_low": Decimal("99"),
        "entry_zone_high": Decimal("101"),
        "stop_plan": StopPlan(price=Decimal("95"), model="structure"),
        "target_plan": TargetPlan(
            price=Decimal(110),
            model="test",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
        "quote": Quote(
            symbol="AAPL",
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            ts=datetime(2026, 3, 10, 15, 0, 55, tzinfo=UTC),
            source="test",
        ),
        "bars_count": 60,
        "bar_timeframe": "1Day",
        "last_bar_ts": datetime(2026, 3, 10, 14, 0, tzinfo=UTC),
        "market": MarketAssessment(
            regime="risk_on",
            score=80,
            risk_posture="risk_on",
            reasons=["test"],
            evaluated_at=datetime(2026, 3, 10, 15, 0, tzinfo=UTC),
            benchmark="SPY",
        ),
        "sector_label": "technology",
        "sector_tradable": True,
        "sector_benchmark": "XLK",
        "sector_provider": "test",
        "sector_source_ts": datetime(2026, 3, 10, 15, 0, tzinfo=UTC),
        "news_status": NewsCheck.CHECKED,
        "earnings_status": EarningsCheck.CHECKED,
        "strategy_version": "test@1",
        "admission_version": "admission@1",
        "policy_version": "entry_policy@1",
        "aggressiveness": 0,
        "geometry_hash": "geo1",
        "evaluated_at": datetime(2026, 3, 10, 15, 1, tzinfo=UTC),
        "portfolio_snapshot": {"equity": "100000"},
        "risk_snapshot": {"verdict": "pass", "sized_qty": "10"},
        "liquidity_snapshot": {"ok": True},
        "decision_version": 0,
        "sized_qty": Decimal(10),
        "limit_price": Decimal(100),
        "stop_price": Decimal(95),
    }
    base.update(overrides)
    return AdmissionInput(**base)


def test_sealed_evidence_refuses_authority_model_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.enums import DataHealthStatus
    from trading.trade_admission import evaluate_from_admission_input

    rid = uuid4()
    inp = _input(request_id=rid, opportunity_id=uuid4())
    cmd = ApprovalCommand(
        request_id=rid,
        opportunity_id=inp.opportunity_id,
        expected_decision_version=0,
        requested_at=datetime(2026, 3, 10, 15, 1, tzinfo=UTC),
    )
    allowed = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=80,
        entry_quality=80,
        effective_rr=2.5,
        chase_score=10,
        structure_valid=True,
        stop_valid=True,
        target_valid=True,
        data_status=DataHealthStatus.HEALTHY,
        reason_codes=["BUY_ALLOWED"],
        admission_version="admission@1",
    )
    monkeypatch.setattr(
        "trading.approval_evidence.evaluate_from_admission_input",
        lambda *_a, **_k: allowed,
    )
    result = evaluate_final_approval(
        command=cmd,
        admission_input=inp,
        geometry_hash="geo1",
        sized_qty=Decimal(10),
        limit_price=Decimal(100),
        stop_price=Decimal(95),
        risk_verdict="pass",
        liquidity_ok=True,
        broker="MockPaperBroker",
        broker_account_id="mock-paper-account",
        broker_environment="paper",
    )
    with pytest.raises(TypeError, match="sealed"):
        result.evidence.model_copy(update={"geometry_hash": "other"})
    assert result.fingerprint
    assert result.evidence.request_fingerprint == result.fingerprint
    assert len(result.fingerprint) == 32


@pytest.mark.asyncio
async def test_hundred_distinct_request_ids_second_blocked_while_in_flight() -> None:
    """100 different request_ids → ≤1 BUY; losers ENTRY_IN_FLIGHT or idempotent EXECUTED."""
    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = await _approved_opp(broker, store)
    version = opp.decision_version
    barrier = asyncio.Barrier(100)

    async def _one() -> str:
        await barrier.wait()
        try:
            result = await _service(broker, store, intents).decide(
                opp.id,
                UserDecision.APPROVE,
                request_id=uuid4(),
                expected_decision_version=version,
            )
            return result.status.value
        except EntryInFlightError:
            return "in_flight"
        except (ApprovalDomainError, ValueError, RuntimeError) as exc:
            return type(exc).__name__

    outcomes = await asyncio.gather(*[_one() for _ in range(100)])
    buy_calls = [
        c
        for c in place.call_args_list
        if getattr(c.args[0], "side", None) is OrderSide.BUY
        or getattr(getattr(c.args[0], "side", None), "value", None) == "buy"
    ]
    assert len(buy_calls) <= 1
    entries = intents.list_by_key_prefix(f"entry:{opp.id}:")
    assert len(entries) <= 1
    # After the winner fills, decide() returns EXECUTED idempotently; during the
    # race losers must not mint a second intent/order.
    assert set(outcomes) <= {"executed", "in_flight", "ValueError", "RuntimeError"}
    assert outcomes.count("executed") >= 1


@pytest.mark.asyncio
async def test_sql_commit_rollback_leaves_no_partial_bundle(monkeypatch) -> None:
    """Inject failure after intent insert → no durable ApprovalBundle remains."""
    from core.enums import TradingMode
    from risk.risk_engine import RiskEngine
    from tests.support import CLEARED_EARNINGS, admission_ready_candidate
    from trading import approval_commit as ac
    from trading.intents import OrderIntentStore
    from trading.opportunities import OpportunityStore

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    opp_store = OpportunityStore(engine)
    intent_store = OrderIntentStore(engine)
    adm = AdmissionRecordStore(engine=engine)
    broker = MockPaperBroker()
    cand = admission_ready_candidate(symbol="AAPL")
    risk = RiskEngine().evaluate(cand, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = opp_store.create(cand, risk, TradingMode.CONFIRMATION)
    rid = uuid4()
    admission = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=80,
        entry_quality=80,
        effective_rr=2.0,
        chase_score=10,
        structure_valid=True,
        stop_valid=True,
        target_valid=True,
        reason_codes=["BUY_ALLOWED"],
        admission_version="admission@1",
    )
    inp = _input(opportunity_id=opp.id, request_id=rid)
    fp = build_request_fingerprint(
        inp, geometry_hash="geo1", decision_version=0, request_id=rid, sized_qty=10, limit_price=100
    )

    original = ac._intent_in_session

    def _boom(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected_crash_after_intent")

    monkeypatch.setattr(ac, "_intent_in_session", _boom)
    with pytest.raises(RuntimeError, match="injected_crash"):
        commit_approval_bundle(
            opportunity_id=opp.id,
            admission=admission,
            admission_input=inp,
            geometry_hash="geo1",
            quote_ts=inp.quote.ts,
            market_gate_ts=None,
            pipeline_run_id=None,
            broker_name="MockPaperBroker",
            qty=Decimal(10),
            limit_px=Decimal(100),
            stop_px=Decimal(95),
            risk_snapshot={"verdict": "pass"},
            strategy_version="test@1",
            symbol="AAPL",
            opportunity_store=opp_store,
            intent_store=intent_store,
            admission_store=adm,
            decision_version=0,
            request_id=rid,
            request_fingerprint=fp,
            broker_account_id="MockPaperBroker:paper",
        )
    assert intent_store.list_by_key_prefix(f"entry:{opp.id}:") == []
    refreshed = opp_store.get(opp.id)
    assert refreshed is not None
    assert refreshed.approval_admission_record_id is None
