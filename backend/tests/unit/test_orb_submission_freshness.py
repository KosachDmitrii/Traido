"""Time spent committing approval cannot make stale evidence executable."""

from datetime import timedelta
from uuid import uuid4

import pytest

from core.audit import InMemoryAudit
from core.enums import UserDecision
from tests.unit.test_entry_gate_enforcement import SESSION, _Bars, _setup
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.intents import MemoryOrderIntentStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]


async def test_quote_expiring_during_submit_audit_never_reaches_broker():
    broker, store, opp = await _setup()
    now = [SESSION]

    class SlowAudit(InMemoryAudit):
        async def append(self, event_type, *args, **kwargs):
            result = await super().append(event_type, *args, **kwargs)
            if event_type == "OrderSubmitStarted":
                now[0] += timedelta(seconds=6)
            return result

    intents = MemoryOrderIntentStore()
    service = ExecutionService(
        broker=broker,
        store=store,
        audit=SlowAudit(),
        intents=intents,
        exit_store=MemoryExitStore(),
        market_data=_Bars(volume=5_000_000),
        clock=lambda: now[0],
    )
    with pytest.raises(RuntimeError, match="ORB_SUBMISSION_BLOCKED:ORB_QUOTE_STALE"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.orders == []
    entries = intents.list_by_key_prefix("entry:")
    assert len(entries) == 1
    assert entries[0].status.value == "rejected"


@pytest.mark.parametrize("mode", ["above", "wide", "missing", "stale", "valid", "audit_delay"])
async def test_changed_quote_after_approval_checked_at_broker_boundary(mode):
    from decimal import Decimal

    from broker.paper.mock import MockPaperBroker
    from core.enums import TradingMode
    from risk.risk_engine import RiskEngine
    from strategy.orb import VERSION
    from tests.orb_support import orb_ready_candidate
    from tests.support import CLEARED_EARNINGS, admission_ready_candidate, liquid_market_data
    from trading.opportunities import MemoryOpportunityStore

    broker = MockPaperBroker()
    card = orb_ready_candidate(admission_ready_candidate(), version=VERSION)
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    market = liquid_market_data(price=float(card.entry) - 0.02)
    original = market.get_quote
    now = [SESSION]
    phase = [False]

    async def quote(symbol):
        q = await original(symbol)
        if not phase[0]:
            return q
        if mode == "missing":
            return None
        if mode == "above":
            return q.model_copy(update={"bid": card.entry, "ask": card.entry + Decimal("0.01")})
        if mode == "wide":
            return q.model_copy(update={"bid": card.entry - Decimal("0.40"), "ask": card.entry})
        if mode == "stale":
            return q.model_copy(update={"ts": SESSION - timedelta(seconds=6)})
        return q

    market.get_quote = quote

    class Audit(InMemoryAudit):
        async def append(self, event_type, *args, **kwargs):
            result = await super().append(event_type, *args, **kwargs)
            if event_type == "OrderSubmitStarted":
                phase[0] = True
            if event_type == "EntrySubmissionPriceChecked" and mode == "audit_delay":
                now[0] += timedelta(seconds=6)
            return result

    audit = Audit()
    intents = MemoryOrderIntentStore()
    service = ExecutionService(
        broker=broker,
        store=store,
        audit=audit,
        intents=intents,
        exit_store=MemoryExitStore(),
        market_data=market,
        clock=lambda: now[0],
    )

    async def approve():
        return await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    if mode == "valid":
        await approve()
        buys = [o for o in broker.orders if o.side.value == "buy"]
        assert len(buys) == 1
        assert buys[0].limit_price == card.entry
    else:
        expected = {
            "above": "ORB_WAITING_PULLBACK",
            "wide": "SPREAD_TOO_WIDE",
            "missing": "ORB_QUOTE_MISSING",
            "stale": "ORB_QUOTE_STALE",
            "audit_delay": "ORB_QUOTE_STALE",
        }[mode]
        with pytest.raises(RuntimeError, match=f"ORB_SUBMISSION_BLOCKED:.*{expected}"):
            await approve()
        assert broker.orders == []
        assert intents.list_by_key_prefix("entry:")[0].status.value == "rejected"
    assert any(e["event_type"] == "EntrySubmissionPriceChecked" for e in audit.events)
