from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.audit import InMemoryAudit
from core.enums import IntentStatus, OrderSide, OrderStatus, OrderType, Timeframe, TradingMode
from core.schemas import OrderRecord
from risk.risk_engine import RiskEngine
from tests.unit.test_capital_safety import _candidate, _portfolio
from trading import entry_recovery as recovery
from trading.entry_activity import track_entry
from trading.geometry_hash import compute_geometry_hash
from trading.intents import MemoryOrderIntentStore
from trading.ledger import PositionLedger
from trading.opportunities import MemoryOpportunityStore
from trading.order_intent import OrderIntent
from trading.reconcile import ReconciliationReport, reconcile_protective_orders


@pytest.fixture
def case(monkeypatch):
    candidate = _candidate().model_copy(update={"exec_timeframe": Timeframe.H1})
    opp = MemoryOpportunityStore().create(
        candidate, RiskEngine().evaluate(candidate, _portfolio()), TradingMode.CONFIRMATION
    )
    admission_id = uuid4()
    geometry = compute_geometry_hash(
        entry=candidate.entry,
        stop=candidate.stop,
        target=candidate.target,
        exec_timeframe=candidate.exec_timeframe.value,
        strategy_version=candidate.strategy_version,
    )
    opp = opp.model_copy(
        update={"approval_admission_record_id": admission_id, "geometry_hash": geometry}
    )
    record = SimpleNamespace(
        id=admission_id,
        admitted=True,
        phase="approval",
        symbol=candidate.symbol,
        opportunity_id=opp.id,
        geometry_hash=geometry,
    )
    monkeypatch.setattr(recovery.OPPORTUNITIES, "get", lambda _: opp)
    monkeypatch.setattr(recovery.ADMISSION_RECORDS, "get", lambda _: record)
    broker = SimpleNamespace(environment="paper", account_id="DU123")
    order = OrderRecord(
        id=uuid4(),
        client_order_id="traido-e-recover",
        broker_order_id="123",
        symbol=candidate.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal(10),
        filled_qty=Decimal(10),
        filled_avg_price=candidate.entry,
        status=OrderStatus.FILLED,
    )
    broker.get_order = AsyncMock(return_value=order)
    broker.list_open_orders = AsyncMock(return_value=[])
    positions = {candidate.symbol: SimpleNamespace(qty=Decimal(10))}
    broker.list_positions = AsyncMock(
        return_value=[SimpleNamespace(symbol=candidate.symbol, qty=Decimal(10))]
    )
    broker.place_order = AsyncMock(side_effect=AssertionError("No new entry"))
    intents = MemoryOrderIntentStore()
    intent, _ = intents.create_or_get(
        OrderIntent(
            idempotency_key=f"entry:{opp.id}:0",
            broker="SimpleNamespace",
            broker_account_id="DU123",
            broker_environment="paper",
            opportunity_id=opp.id,
            symbol=candidate.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            requested_qty=Decimal(10),
            status=IntentStatus.FILLED,
            client_order_id=order.client_order_id,
            broker_order_id="123",
            approval_admission_record_id=admission_id,
            geometry_hash=geometry,
        )
    )
    return SimpleNamespace(
        broker=broker,
        order=order,
        intents=intents,
        intent=intent,
        ledger=PositionLedger(),
        positions=positions,
        record=record,
        opp=opp,
    )


async def run(c):
    report = ReconciliationReport()
    await recovery.recover_entry_positions(
        c.broker, c.intents, c.ledger, c.positions, report, InMemoryAudit()
    )
    return report


@pytest.mark.asyncio
async def test_terminal_fill_recovers_once_and_protection_stays_visible(case):
    report = await run(case)
    rows = case.ledger.get_open()
    assert len(rows) == 1
    assert rows[0].target_price == case.opp.candidate.target
    assert rows[0].stop_price == case.opp.candidate.stop
    assert case.intents.get(case.intent.id).position_id == rows[0].id
    assert report.unresolved
    case.ledger = PositionLedger()  # durable reload, not an in-memory recovery flag
    await run(case)
    assert len(case.ledger.get_open()) == 1
    report = ReconciliationReport()
    await reconcile_protective_orders(case.broker, case.ledger, report=report)
    assert report.unresolved  # no installer => protection cannot disappear as OK
    case.broker.place_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["account", "geometry", "quantity", "approval", "orders"])
async def test_unverifiable_evidence_never_creates_position(case, fault):
    if fault == "account":
        case.broker.account_id = "DUOTHER"
    elif fault == "geometry":
        case.record.geometry_hash = "changed"
    elif fault == "quantity":
        case.positions[case.opp.candidate.symbol].qty = Decimal(9)
    elif fault == "approval":
        case.record.admitted = False
    else:
        case.broker.list_open_orders.side_effect = RuntimeError("offline")
    assert (await run(case)).unresolved
    assert case.ledger.get_open() == []
    case.broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_adopts_exact_existing_stop(case):
    stop = case.order.model_copy(
        update={
            "side": OrderSide.SELL,
            "order_type": OrderType.STOP,
            "status": OrderStatus.ACCEPTED,
            "filled_qty": Decimal(0),
            "broker_order_id": "stop-1",
            "stop_price": case.opp.candidate.stop,
        }
    )
    case.broker.list_open_orders.return_value = [stop]
    await run(case)
    row = case.ledger.get_open()[0]
    assert row.payload["stop_order_id"] == "stop-1"
    installer = SimpleNamespace(
        ensure_protection=AsyncMock(side_effect=AssertionError("Duplicate stop"))
    )
    await reconcile_protective_orders(case.broker, case.ledger, execution=installer)
    installer.ensure_protection.assert_not_called()


@pytest.mark.asyncio
async def test_active_execution_is_not_recovered(case):
    class Worker:
        @track_entry
        async def decide(self, opportunity_id):
            await run(case)
            assert case.ledger.get_open() == []

    await Worker().decide(case.opp.id)
    await run(case)
    assert len(case.ledger.get_open()) == 1


@pytest.mark.asyncio
async def test_closed_entry_never_resurrects(case):
    await run(case)
    case.ledger.close_and_journal(
        symbol=case.opp.candidate.symbol,
        exit_price=case.opp.candidate.entry,
        exit_reasons=["closed"],
        qty=Decimal(10),
    )
    await run(case)
    assert case.ledger.get_open() == []
