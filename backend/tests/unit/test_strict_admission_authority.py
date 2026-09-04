"""Strict ApprovalAdmission authority — acceptance suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.market.agent import assess_market
from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    AdmissionDecision,
    DataHealthStatus,
    IntentPurpose,
    IntentStatus,
    OpportunityStatus,
    OrderSide,
    TradingMode,
    UserDecision,
)
from core.schemas import AdmissionRecord, Quote
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, admission_ready_candidate, liquid_market_data
from trading.admission_authority import AdmissionAuthorityError, assert_authority_invariant
from trading.approval_errors import ApprovalDomainError, StaleDecisionError
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore
from trading.order_intent import OrderIntent

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]


@pytest.fixture(autouse=True)
def _kill_off() -> None:
    set_kill_switch(False)


def _service(broker, store, intents, audit=None):
    return ExecutionService(
        broker=broker,
        audit=audit or InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=intents,
        market_data=liquid_market_data(),
        require_rth=False,
        require_fresh_reconciliation=False,
    )


async def _approved_opp(broker, store, symbol="AAPL"):
    cand = admission_ready_candidate(symbol=symbol)
    risk = RiskEngine().evaluate(cand, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    return store.create(cand, risk, TradingMode.CONFIRMATION)


def _record_for(
    opp,
    *,
    phase="approval",
    opportunity_id=None,
    geometry_hash="abc123",
    data_status=DataHealthStatus.HEALTHY,
    decision=AdmissionDecision.BUY_ALLOWED,
    admitted=True,
    expires_at=None,
):
    now = datetime.now(UTC)
    return AdmissionRecord(
        id=uuid4(),
        symbol=opp.candidate.symbol,
        recorded_at=now,
        decision=decision,
        admitted=admitted,
        data_status=data_status,
        phase=phase,
        opportunity_id=opportunity_id if opportunity_id is not None else opp.id,
        geometry_hash=geometry_hash,
        expires_at=expires_at or (now + timedelta(minutes=15)),
        context={"phase": phase, "request_fingerprint": "fp"},
        request_fingerprint="fp",
    )


def _intent_for(opp, record, *, qty=Decimal(10), geometry_hash="abc123"):
    return OrderIntent(
        idempotency_key=f"entry:{opp.id}:0",
        broker="MockPaperBroker",
        broker_account_id="MockPaperBroker:paper",
        broker_environment="paper",
        symbol=opp.candidate.symbol,
        side="buy",
        requested_qty=qty,
        order_type="limit",
        limit_price=Decimal(100),
        stop_price=Decimal(95),
        strategy_version="test@1",
        opportunity_id=opp.id,
        approval_admission_record_id=record.id,
        geometry_hash=geometry_hash,
        request_fingerprint=record.request_fingerprint,
        purpose=IntentPurpose.ENTRY,
        status=IntentStatus.CREATED,
    )


@pytest.mark.asyncio
async def test_null_phase_blocks_broker() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    opp = await _approved_opp(broker, store)
    rec = _record_for(opp, phase=None)  # type: ignore[arg-type]
    # force null phase
    rec = rec.model_copy(update={"phase": None, "context": {}})
    opp = opp.model_copy(update={"approval_admission_record_id": rec.id, "geometry_hash": "abc123"})
    intent = _intent_for(opp, rec)
    with pytest.raises(AdmissionAuthorityError, match="wrong_phase"):
        assert_authority_invariant(rec, opp, intent)


@pytest.mark.asyncio
async def test_null_opportunity_id_blocks_broker() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    opp = await _approved_opp(broker, store)
    rec = _record_for(opp)
    rec = rec.model_copy(update={"opportunity_id": None})
    opp = opp.model_copy(update={"approval_admission_record_id": rec.id, "geometry_hash": "abc123"})
    intent = _intent_for(opp, rec)
    with pytest.raises(AdmissionAuthorityError, match="opportunity_id_null"):
        assert_authority_invariant(rec, opp, intent)


@pytest.mark.asyncio
async def test_geometry_hash_mismatch_blocks_broker() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    opp = await _approved_opp(broker, store)
    rec = _record_for(opp, geometry_hash="aaa")
    opp = opp.model_copy(update={"approval_admission_record_id": rec.id, "geometry_hash": "bbb"})
    intent = _intent_for(opp, rec, geometry_hash="aaa")
    with pytest.raises(AdmissionAuthorityError, match="GEOMETRY_MISMATCH"):
        assert_authority_invariant(rec, opp, intent)


@pytest.mark.asyncio
async def test_degraded_data_status_blocks_broker() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    opp = await _approved_opp(broker, store)
    rec = _record_for(opp, data_status=DataHealthStatus.DEGRADED)
    opp = opp.model_copy(update={"approval_admission_record_id": rec.id, "geometry_hash": "abc123"})
    intent = _intent_for(opp, rec)
    with pytest.raises(AdmissionAuthorityError, match="BUY_REJECTED_ADMISSION"):
        assert_authority_invariant(rec, opp, intent)


@pytest.mark.asyncio
async def test_fk_mismatch_blocks_broker() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    opp = await _approved_opp(broker, store)
    rec = _record_for(opp)
    opp = opp.model_copy(
        update={"approval_admission_record_id": uuid4(), "geometry_hash": "abc123"}
    )
    intent = _intent_for(opp, rec)
    with pytest.raises(AdmissionAuthorityError, match="opportunity_fk_mismatch"):
        assert_authority_invariant(rec, opp, intent)


@pytest.mark.asyncio
async def test_approve_zero_broker_when_authority_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2024, 6, 3, 15, 30, tzinfo=UTC)
    monkeypatch.setattr("trading.execution._utcnow", lambda: now)
    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = await _approved_opp(broker, store)

    # After a successful-looking commit path, corrupt the chain before place.
    from trading import approval_commit as ac

    real = ac.commit_approval_bundle

    def _corrupt(**kwargs):
        bundle = real(**kwargs)
        bad = bundle.opportunity.model_copy(update={"geometry_hash": "tampered"})
        return ac.ApprovalBundle(
            opportunity=bad,
            admission_record=bundle.admission_record,
            intent=bundle.intent,
            created_intent=bundle.created_intent,
        )

    monkeypatch.setattr(ac, "commit_approval_bundle", _corrupt)
    with pytest.raises((AdmissionAuthorityError, RuntimeError)):
        await _service(broker, store, intents).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert place.call_count == 0


@pytest.mark.asyncio
async def test_lost_reply_retry_reuses_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2024, 6, 3, 15, 30, tzinfo=UTC)
    monkeypatch.setattr("trading.execution._utcnow", lambda: now)
    set_kill_switch(False)

    class LostReply(MockPaperBroker):
        def __init__(self) -> None:
            super().__init__()
            self.submit_count = 0

        async def place_order(self, request):  # type: ignore[no-untyped-def]
            self.submit_count += 1
            if self.submit_count == 1:
                raise RuntimeError("lost reply")
            return await super().place_order(request)

    broker = LostReply()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    opp = await _approved_opp(broker, store)

    with pytest.raises(RuntimeError, match="ENTRY_STATE_UNKNOWN"):
        await _service(broker, store, intents, audit).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.submit_count == 1
    assert len(intents.list_by_key_prefix(f"entry:{opp.id}:")) == 1

    store.release_stale_approving(older_than_sec=0)
    # Second attempt: intent unresolved with same fingerprint — must not place again
    # until recovery; may raise ENTRY_STATE_UNKNOWN again without a second submit.
    before = broker.submit_count
    with pytest.raises(RuntimeError):
        await _service(broker, store, intents, audit).decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.submit_count == before  # no second place while UNKNOWN
    assert len(intents.list_by_key_prefix(f"entry:{opp.id}:")) == 1


@pytest.mark.asyncio
async def test_stale_unresolved_intent_different_hash_raises() -> None:
    from core.enums import EntryDecision, InstrumentThesis, SetupType, TargetReachabilityClass
    from core.schemas import (
        AdmissionInput,
        EntryDecisionBundle,
        EntryQualityBreakdown,
        EntryTimingFacts,
        StopPlan,
        TargetPlan,
    )
    from trading.approval_commit import commit_approval_bundle

    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = await _approved_opp(broker, store)

    # Seed an unresolved intent with a different geometry hash.
    ghost = OrderIntent(
        idempotency_key=f"entry:{opp.id}:0",
        broker="MockPaperBroker",
        symbol="AAPL",
        side="buy",
        requested_qty=Decimal(10),
        order_type="limit",
        limit_price=Decimal(100),
        stop_price=Decimal(95),
        strategy_version="test@1",
        opportunity_id=opp.id,
        approval_admission_record_id=uuid4(),
        geometry_hash="old_hash",
        purpose=IntentPurpose.ENTRY,
        status=IntentStatus.UNKNOWN,
    )
    intents.create_or_get(ghost)

    facts = EntryTimingFacts(current_price=100.0, atr=2.0, stop_distance_atr=2.0)
    bundle = EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=80,
        setup_quality=80,
        breakdown=EntryQualityBreakdown(
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
        ),
        facts=facts,
        stop_price=Decimal(95),
        target=TargetPlan(
            price=Decimal(115), model="structure", reachability=TargetReachabilityClass.REALISTIC
        ),
    )
    quote = Quote(
        symbol="AAPL",
        bid=Decimal("99.9"),
        ask=Decimal("100.1"),
        ts=datetime.now(UTC),
        source="test",
    )
    from trading.trade_admission import ADMISSION_VERSION, POLICY_VERSION

    admission_input = AdmissionInput(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=80,
        stop_plan=StopPlan(price=Decimal(95)),
        target_plan=bundle.target,
        quote=quote,
        bars_count=60,
        bar_timeframe="1Hour",
        strategy_version="test@1",
        admission_version=ADMISSION_VERSION,
        policy_version=POLICY_VERSION,
        aggressiveness=0,
        geometry_hash="new_hash",
        evaluated_at=datetime.now(UTC),
        opportunity_id=opp.id,
    )
    from core.schemas import TradeAdmissionResult

    admission = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        data_status=DataHealthStatus.HEALTHY,
        setup_quality=80,
        entry_quality=80,
    )
    store.claim(
        opp.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.APPROVING,
    )
    with pytest.raises(StaleDecisionError):
        commit_approval_bundle(
            opportunity_id=opp.id,
            admission=admission,
            admission_input=admission_input,
            geometry_hash="new_hash",
            quote_ts=quote.ts,
            market_gate_ts=None,
            pipeline_run_id=None,
            broker_name="MockPaperBroker",
            qty=Decimal(10),
            limit_px=Decimal("100.1"),
            stop_px=Decimal(95),
            risk_snapshot={},
            strategy_version="test@1",
            symbol="AAPL",
            opportunity_store=store,
            intent_store=intents,
            decision_version=0,
            broker_account_id="MockPaperBroker:paper",
        )


@pytest.mark.asyncio
async def test_empty_fred_series_is_data_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(_client, _key, _series):
        return None

    monkeypatch.setattr("agents.market.agent._fred_latest", _empty)
    result = await assess_market("fake-key")
    assert result.evaluated_at is None
    assert result.sector_tradable is None
    assert "FRED_SERIES_EMPTY" in result.reasons
    assert "DATA_BLOCKED" in result.reasons


@pytest.mark.asyncio
async def test_fred_without_key_is_data_blocked() -> None:
    result = await assess_market(None)
    assert result.evaluated_at is None
    assert result.sector_tradable is None
    assert "FRED_NOT_CONFIGURED" in result.reasons


@pytest.mark.asyncio
async def test_current_ten_year_is_not_risk_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _latest(_client, _key, series):
        return {"DGS10": 4.79, "UNRATE": 4.10}[series]

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key")
    assert result.regime.value == "neutral"
    assert result.risk_posture == "neutral"
    assert result.evaluated_at is not None


@pytest.mark.asyncio
async def test_elevated_ten_year_is_risk_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _latest(_client, _key, series):
        return {"DGS10": 5.50, "UNRATE": 4.10}[series]

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key")
    assert result.regime.value == "risk_off"
    assert result.risk_posture == "risk_off"


def test_sql_no_entry_intent_without_approval_fk() -> None:
    from database.models.desk import OrderIntentRow
    from database.session import session_factory

    SessionLocal = session_factory()
    with SessionLocal() as session:
        bad = (
            session.query(OrderIntentRow)
            .filter(
                OrderIntentRow.purpose == "entry",
                (
                    (OrderIntentRow.approval_admission_record_id.is_(None))
                    | (OrderIntentRow.geometry_hash.is_(None))
                ),
            )
            .count()
        )
    assert bad == 0


@pytest.mark.asyncio
async def test_hundred_concurrent_approves_one_buy() -> None:
    """100 concurrent APPROVE with the same request_id → ≤1 broker BUY."""
    import asyncio
    from uuid import uuid4

    from core.enums import UserDecision

    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = await _approved_opp(broker, store)
    rid = uuid4()
    version = opp.decision_version

    async def _one() -> None:
        try:
            await _service(broker, store, intents).decide(
                opp.id,
                UserDecision.APPROVE,
                request_id=rid,
                expected_decision_version=version,
            )
        except (ValueError, RuntimeError):
            # Losers: claim races, entry-in-flight, already executed, etc.
            return
        except ApprovalDomainError:
            return

    await asyncio.gather(*[_one() for _ in range(100)])
    buy_calls = [
        c
        for c in place.call_args_list
        if getattr(c.args[0], "side", None) is OrderSide.BUY
        or getattr(getattr(c.args[0], "side", None), "value", None) == "buy"
    ]
    assert len(buy_calls) <= 1
    entries = intents.list_by_key_prefix(f"entry:{opp.id}:")
    assert len(entries) <= 1
