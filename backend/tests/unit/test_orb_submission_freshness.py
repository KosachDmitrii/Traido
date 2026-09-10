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
