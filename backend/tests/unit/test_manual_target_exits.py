"""Owner exit selection: real stop cancellation and original targets across days."""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from core.audit import InMemoryAudit
from core.config import get_settings
from core.enums import OrderSide, OrderStatus, OrderType
from core.schemas import OrderRecord
from strategy.orb.loop import exit_due_positions
from tests.unit.test_orb_policy import quote
from tests.unit.test_orb_retest import ready_plan
from trading.exit_policy import manual_target_exits
from trading.reconcile import ReconciliationReport, reconcile_protective_orders


@pytest.fixture
def owner_policy(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    assert manual_target_exits()


@pytest.mark.usefixtures("owner_policy")
@pytest.mark.parametrize(
    "days,bid,stale,target_missing,expected",
    [
        (0, "100.00", False, False, 0),
        (1, "100.00", False, False, 0),
        (3, "102.49", False, False, 0),
        (3, "102.50", False, False, 1),
        (3, "102.60", True, False, 0),
        (3, "102.60", False, True, 0),
    ],
)
async def test_original_target_survives_time_and_day_change(
    monkeypatch, days, bid, stale, target_missing, expected
):
    from api import deps
    from trading.ledger import LEDGER

    p, _, opened = ready_plan()
    now = opened + timedelta(days=days, minutes=40)
    raw = p.model_dump(mode="json")
    if target_missing:
        raw["evidence"].pop("retest")
    row = SimpleNamespace(
        symbol=p.symbol,
        qty=D(5),
        avg_entry=D("101.12"),
        opened_at=opened,
        payload={"exit_policy": "session_close", "exit_at": p.exit_at.isoformat(), "orb_plan": raw},
    )
    monkeypatch.setattr(LEDGER, "get_open", lambda: [row])
    execution = SimpleNamespace(
        quotes=SimpleNamespace(
            get_quote=AsyncMock(
                return_value=quote(
                    bid, str(D(bid) + D("0.02")), now - timedelta(seconds=6 if stale else 0)
                )
            )
        ),
        audit=InMemoryAudit(),
        close_position=AsyncMock(),
    )
    monkeypatch.setattr(deps, "build_execution_service", lambda: execution)
    assert await exit_due_positions(now=now) == expected
    if expected:
        execution.close_position.assert_awaited_once_with(p.symbol, reason="ORB_TARGET_REACHED")
    else:
        execution.close_position.assert_not_awaited()


@pytest.mark.usefixtures("owner_policy")
@pytest.mark.parametrize("cancelled", [False, True])
async def test_existing_stop_cancelled_but_never_reinstalled(cancelled):
    row = SimpleNamespace(symbol="AAPL", payload={"stop_order_id": "owned"})
    stop = OrderRecord(
        id=uuid4(),
        client_order_id="protection-owned",
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        qty=D(5),
        stop_price=D(99),
        broker_order_id="owned",
        status=OrderStatus.SUBMITTED,
    )
    unrelated = stop.model_copy(update={"broker_order_id": "external"})
    broker = SimpleNamespace(list_open_orders=AsyncMock(return_value=[stop, unrelated]))
    ledger = SimpleNamespace(get_open=lambda: [row])
    execution = SimpleNamespace(
        cancel_protection=AsyncMock(return_value=cancelled), ensure_protection=AsyncMock()
    )
    report = ReconciliationReport()
    await reconcile_protective_orders(broker, ledger, execution=execution, report=report)
    execution.cancel_protection.assert_awaited_once_with(
        broker_order_id="owned", symbol="AAPL", reason="owner_manual_target_exit_policy"
    )
    execution.ensure_protection.assert_not_awaited()
    assert bool(report.unresolved) is (not cancelled)


@pytest.mark.usefixtures("owner_policy")
async def test_protection_and_emergency_paths_do_not_submit():
    from broker.paper.mock import MockPaperBroker
    from trading.execution import ExecutionService
    from trading.exits import MemoryExitStore
    from trading.opportunities import MemoryOpportunityStore

    broker = MockPaperBroker()
    service = ExecutionService(
        broker=broker,
        store=MemoryOpportunityStore(),
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )
    assert (
        await service.ensure_protection(symbol="AAPL", qty=D(5), stop_price=D(99), reason="missing")
        is None
    )
    assert (
        await service.resize_protection(
            symbol="AAPL", position_id=None, remaining_qty=D(3), stop_price=D(99), reason="partial"
        )
        is None
    )
    assert not await service._emergency_flatten(
        symbol="AAPL", qty=D(5), pipeline_run_id=None, reason="missing_stop"
    )
    with pytest.raises(RuntimeError, match="AUTOMATIC_EXIT_DISABLED_BY_OWNER"):
        await service.close_position("AAPL", reason="ORB_SESSION_END")
    assert broker.orders == []


@pytest.mark.parametrize("filled", [None, D(0), D(2)])
async def test_stop_cancellation_does_not_retire_a_raced_or_unknown_fill(filled):
    from trading.execution import ExecutionService
    from trading.opportunities import MemoryOpportunityStore

    broker = SimpleNamespace(get_order=AsyncMock(return_value=SimpleNamespace(filled_qty=filled)))
    service = ExecutionService(broker=broker, store=MemoryOpportunityStore(), audit=InMemoryAudit())
    service._cancel_and_await_gone = AsyncMock(return_value=True)
    service._retire_protective_intent = Mock()
    assert await service.cancel_protection(
        broker_order_id="stop-1", symbol="AAPL", reason="owner"
    ) is (filled == D(0))
    assert service._retire_protective_intent.call_count == (1 if filled == D(0) else 0)
