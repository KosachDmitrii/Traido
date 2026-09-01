"""Operator may approve fewer shares than Risk sized — never more."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OpportunityStatus, TradeAction, TradingMode, UserDecision
from core.schemas import TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ExecutionService
from trading.opportunities import MemoryOpportunityStore
from trading.pricing import round_order_qty


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


@pytest.mark.asyncio
async def test_operator_may_buy_fewer_shares_than_risk_max() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    assert risk.sized_qty is not None
    max_qty = round_order_qty(risk.sized_qty)
    assert max_qty >= 2

    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
    )
    result = await service.decide(opp.id, UserDecision.APPROVE, qty=Decimal(1))
    assert result.status == OpportunityStatus.EXECUTED
    assert result.approved_qty == Decimal(1)
    buys = [o for o in broker.orders if o.side.value == "buy"]
    assert buys and buys[0].qty == Decimal(1)


@pytest.mark.asyncio
async def test_operator_qty_above_risk_max_is_refused() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    assert risk.sized_qty is not None
    max_qty = round_order_qty(risk.sized_qty)
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
    )
    with pytest.raises(RuntimeError, match="OPERATOR_QTY_ABOVE_RISK"):
        await service.decide(opp.id, UserDecision.APPROVE, qty=max_qty + 1)
    assert broker.orders == []
    current = store.get(opp.id)
    assert current is not None
    assert current.status == OpportunityStatus.AWAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_omitted_qty_still_uses_full_risk_size() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    assert risk.sized_qty is not None
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
    )
    result = await service.decide(opp.id, UserDecision.APPROVE)
    assert result.status == OpportunityStatus.EXECUTED
    buys = [o for o in broker.orders if o.side.value == "buy"]
    assert buys and result.approved_qty == buys[0].qty
    assert result.approved_qty is not None and result.approved_qty >= 1
