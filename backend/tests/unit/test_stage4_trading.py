"""Stage 4 — risk, confirm, mock paper execution."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OpportunityStatus, RiskVerdict, TradeAction, TradingMode, UserDecision
from core.schemas import RiskDecision, RiskLimits, TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ExecutionService
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(110),
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
        pipeline_run_id=uuid4(),
    )


def _portfolio(**over):
    from core.schemas import PortfolioSnapshot

    base = {
        "equity": Decimal(100000),
        "cash": Decimal(100000),
        "buying_power": Decimal(100000),
        "open_exposure": Decimal(0),
        "open_positions": 0,
        "day_pnl": Decimal(0),
        "week_pnl": Decimal(0),
        "drawdown_pct": 0.0,
        "kill_switch": False,
    }
    base.update(over)
    return PortfolioSnapshot(**base)


def test_risk_rejects_daily_loss() -> None:
    eng = RiskEngine(RiskLimits(max_daily_loss_pct=2.0))
    decision = eng.evaluate(
        _candidate(),
        _portfolio(day_pnl=Decimal(-3000)),
    )
    assert decision.verdict == RiskVerdict.REJECT
    assert "MAX_DAILY_LOSS" in decision.reasons


def test_risk_sizes_within_position_cap() -> None:
    eng = RiskEngine(RiskLimits(max_risk_per_trade_pct=1.0, max_position_pct=5.0))
    decision = eng.evaluate(_candidate(), _portfolio(), context=CLEARED_EARNINGS)
    assert decision.verdict == RiskVerdict.PASS
    assert decision.sized_qty is not None
    notional = decision.sized_qty * Decimal(100)
    assert notional <= Decimal(100000) * Decimal("0.05") + Decimal("0.01")


@pytest.mark.asyncio
async def test_approve_places_entry_and_stop_on_mock() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    audit = InMemoryAudit()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    assert risk.verdict == RiskVerdict.PASS
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(), broker=broker, audit=audit, store=store
    )
    result = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    assert result.status == OpportunityStatus.EXECUTED
    assert len(broker.orders) >= 2
    sides = {o.side.value for o in broker.orders}
    assert "buy" in sides and "sell" in sides
    assert any(e["event_type"] == "OrderSubmitted" for e in audit.events)


@pytest.mark.asyncio
async def test_skip_does_not_place_orders() -> None:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskDecision(
        verdict=RiskVerdict.PASS,
        reasons=["ok"],
        sized_qty=Decimal(10),
        max_loss_usd=Decimal(50),
        limits_applied=RiskLimits(),
        portfolio=await broker.get_portfolio(),
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(), broker=broker, audit=InMemoryAudit(), store=store
    )
    result = await service.decide(opp.id, UserDecision.SKIP)
    assert result.status == OpportunityStatus.SKIPPED
    assert broker.orders == []


@pytest.mark.asyncio
async def test_kill_switch_blocks_approve() -> None:
    set_kill_switch(True)
    try:
        broker = MockPaperBroker()
        store = MemoryOpportunityStore()
        pass_risk = RiskDecision(
            verdict=RiskVerdict.PASS,
            reasons=["forced"],
            sized_qty=Decimal(1),
            max_loss_usd=Decimal(5),
            limits_applied=RiskLimits(),
            portfolio=await broker.get_portfolio(),
        )
        opp = store.create(_candidate(), pass_risk, TradingMode.CONFIRMATION)
        service = ExecutionService(
            market_data=liquid_market_data(), broker=broker, audit=InMemoryAudit(), store=store
        )
        with pytest.raises(RuntimeError, match="KILL_SWITCH"):
            await service.decide(
                opp.id,
                UserDecision.APPROVE,
                request_id=uuid4(),
                expected_decision_version=opp.decision_version,
            )
    finally:
        set_kill_switch(False)


@pytest.mark.asyncio
async def test_approve_is_idempotent() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(), broker=broker, audit=InMemoryAudit(), store=store
    )
    first = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    n_orders = len(broker.orders)
    second = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    assert first.status == OpportunityStatus.EXECUTED
    assert second.status == OpportunityStatus.EXECUTED
    assert len(broker.orders) == n_orders


@pytest.mark.asyncio
async def test_trading_api_decide_skip() -> None:
    import os

    os.environ["TRAIDO_BROKER_MOCK"] = "true"
    os.environ["TRAIDO_AUTH_DISABLED"] = "true"
    from api.main import app
    from trading.opportunities import OPPORTUNITIES

    broker = MockPaperBroker()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = OPPORTUNITIES.create(_candidate(), risk, TradingMode.CONFIRMATION)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.json()["stage"] >= 4
        resp = await client.post(
            f"/api/v1/opportunities/{opp.id}/decide",
            json={"decision": "skip"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"
    os.environ.pop("TRAIDO_BROKER_MOCK", None)
    os.environ.pop("TRAIDO_AUTH_DISABLED", None)
