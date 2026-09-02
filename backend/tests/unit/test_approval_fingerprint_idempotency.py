"""Fingerprint / request_id idempotency — ApprovalEvidence reuse rules."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from broker.paper.mock import MockPaperBroker
from core.enums import (
    AdmissionDecision,
    EntryDecision,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
    UserDecision,
)
from core.schemas import (
    AdmissionInput,
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
from trading.approval_errors import IdempotencyConflictError, StaleDecisionError
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]


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


def _minimal_input(**overrides) -> AdmissionInput:
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
    )
    base = {
        "bundle": bundle,
        "setup_type": SetupType.PULLBACK_CONTINUATION,
        "setup_quality": 80,
        "target_plan": TargetPlan(
            price=Decimal(110),
            model="test",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
        "quote": Quote(
            symbol="AAPL",
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            ts=datetime(2026, 3, 10, 15, 0, tzinfo=UTC),
            source="test",
        ),
        "bars_count": 60,
        "bar_timeframe": "1Day",
        "last_bar_ts": datetime(2026, 3, 10, 14, 0, tzinfo=UTC),
        "sector_label": "technology",
        "sector_tradable": True,
        "strategy_version": "test@1",
        "admission_version": "admission@1",
        "policy_version": "entry_policy@1",
        "aggressiveness": 0,
        "geometry_hash": "geo1",
        "evaluated_at": datetime(2026, 3, 10, 15, 1, tzinfo=UTC),
        "portfolio_snapshot": {"equity": "100000"},
        "risk_snapshot": {"verdict": "pass", "sized_qty": "10"},
        "liquidity_snapshot": {"ok": True},
    }
    base.update(overrides)
    return AdmissionInput(**base)


def test_fingerprint_includes_sector_and_quote_ts() -> None:
    a = _minimal_input(sector_label="technology")
    b = _minimal_input(sector_label="energy")
    rid = uuid4()
    fa = build_request_fingerprint(a, geometry_hash="geo1", decision_version=0, request_id=rid)
    fb = build_request_fingerprint(b, geometry_hash="geo1", decision_version=0, request_id=rid)
    assert fa != fb

    q2 = _minimal_input(
        quote=Quote(
            symbol="AAPL",
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            ts=datetime(2026, 3, 10, 16, 0, tzinfo=UTC),
            source="test",
        )
    )
    assert build_request_fingerprint(
        a, geometry_hash="geo1", decision_version=0, request_id=rid
    ) != build_request_fingerprint(q2, geometry_hash="geo1", decision_version=0, request_id=rid)


@pytest.mark.asyncio
async def test_same_geometry_different_sector_idempotency_conflict() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    adm = AdmissionRecordStore(engine=engine)
    opp = await _approved_opp(broker, store)
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
    inp1 = _minimal_input(sector_label="technology", opportunity_id=opp.id, request_id=rid)
    fp1 = build_request_fingerprint(
        inp1,
        geometry_hash="geo1",
        decision_version=0,
        request_id=rid,
        sized_qty=10,
        limit_price=100,
    )
    commit_approval_bundle(
        opportunity_id=opp.id,
        admission=admission,
        admission_input=inp1,
        geometry_hash="geo1",
        quote_ts=inp1.quote.ts,
        market_gate_ts=None,
        pipeline_run_id=None,
        broker_name="MockPaperBroker",
        qty=Decimal(10),
        limit_px=Decimal(100),
        stop_px=Decimal(95),
        risk_snapshot={"verdict": "pass"},
        strategy_version="test@1",
        symbol="AAPL",
        opportunity_store=store,
        intent_store=intents,
        admission_store=adm,
        decision_version=0,
        request_id=rid,
        request_fingerprint=fp1,
        broker_account_id="MockPaperBroker:paper",
    )
    store.release_stale_approving(older_than_sec=0)
    inp2 = _minimal_input(sector_label="energy", opportunity_id=opp.id, request_id=rid)
    fp2 = build_request_fingerprint(
        inp2,
        geometry_hash="geo1",
        decision_version=0,
        request_id=rid,
        sized_qty=10,
        limit_price=100,
    )
    assert fp1 != fp2
    with pytest.raises(IdempotencyConflictError):
        commit_approval_bundle(
            opportunity_id=opp.id,
            admission=admission,
            admission_input=inp2,
            geometry_hash="geo1",
            quote_ts=inp2.quote.ts,
            market_gate_ts=None,
            pipeline_run_id=None,
            broker_name="MockPaperBroker",
            qty=Decimal(10),
            limit_px=Decimal(100),
            stop_px=Decimal(95),
            risk_snapshot={"verdict": "pass"},
            strategy_version="test@1",
            symbol="AAPL",
            opportunity_store=store,
            intent_store=intents,
            admission_store=adm,
            decision_version=0,
            request_id=rid,
            request_fingerprint=fp2,
            broker_account_id="MockPaperBroker:paper",
        )


@pytest.mark.asyncio
async def test_stale_decision_version_rejected() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = await _approved_opp(broker, store)
    with pytest.raises(StaleDecisionError):
        await _service(broker, store, intents).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version + 1,
        )
