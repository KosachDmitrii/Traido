"""The desk must be able to close what it opened, without waiting to be asked.

Every exit before this one required the position agent to have raised a card
first, so the only sell button on the desk was a side effect of a judgement the
agent might never make. Correcting the exit rule on 2026-08-31 removed the last
standing card and left four open positions with no way to act on any of them.

The close runs through `decide_exit` rather than beside it, so what is asserted
here is mostly that it inherits: one broker order per request, sizing taken from
the venue rather than the book, and the ledger reduced exactly once.
"""

from __future__ import annotations

from uuid import uuid4

from decimal import Decimal

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OrderSide, TradeAction, TradingMode, UserDecision
from core.schemas import ExitProposal, TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ExecutionService
from trading.exits import EXIT_SOLD, OPERATOR_CLOSE_REASON, MemoryExitStore
from trading.ledger import LEDGER
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]

PRICE = 100.0

def _card() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal("100.00"),
        stop=Decimal("99.00"),
        target=Decimal("102.00"),
        risk_reward=2.0,
        reasons=["test setup"],
        strategy_version="test@1",
    )

async def _service_with_open_position() -> tuple[ExecutionService, MockPaperBroker]:
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    card = _card()
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(price=PRICE),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )
    await service.decide(opp.id, UserDecision.APPROVE, request_id=uuid4(), expected_decision_version=opp.decision_version)
    return service, broker

def _sell_orders(broker: MockPaperBroker):
    return [o for o in broker.orders if o.side is OrderSide.SELL]

@pytest.fixture(autouse=True)
def _armed_desk():
    set_kill_switch(False)
    yield
    set_kill_switch(False)

@pytest.mark.asyncio
async def test_a_position_can_be_closed_without_a_proposal() -> None:
    service, _ = await _service_with_open_position()
    assert LEDGER.find_open_by_symbol("AAPL") is not None

    result = await service.close_position("AAPL")

    assert result.status == EXIT_SOLD
    assert LEDGER.find_open_by_symbol("AAPL") is None

@pytest.mark.asyncio
async def test_the_sell_is_sized_from_the_venue() -> None:
    """Not from the book — the same rule protection already follows."""
    service, broker = await _service_with_open_position()
    held = (await broker.list_positions())[0].qty

    await service.close_position("AAPL")

    market_sells = [o for o in _sell_orders(broker) if o.stop_price is None]
    assert len(market_sells) == 1
    assert market_sells[0].qty == held

@pytest.mark.asyncio
async def test_closing_twice_does_not_sell_twice() -> None:
    """A second click meets the state machine, not the broker."""
    service, broker = await _service_with_open_position()

    await service.close_position("AAPL")
    before = len(_sell_orders(broker))

    with pytest.raises(ValueError, match="no_open_position"):
        await service.close_position("AAPL")

    assert len(_sell_orders(broker)) == before

@pytest.mark.asyncio
async def test_closing_what_is_not_held_is_refused() -> None:
    service, _ = await _service_with_open_position()

    with pytest.raises(ValueError, match="no_open_position:NOPE"):
        await service.close_position("NOPE")

@pytest.mark.asyncio
async def test_the_background_pass_does_not_withdraw_an_operator_card() -> None:
    """`close_position` writes a card and claims it a moment later.

    The position agent withdraws proposals whose reason stopped holding, and it
    has no rule behind this one to consult. Expiring it inside that gap would
    answer the operator's click with a state error they could not explain.
    """
    from agents.position.agent import _withdraw_unsupported
    from trading.exits import EXIT_AWAITING, EXITS

    _, broker = await _service_with_open_position()
    pos = (await broker.list_positions())[0]
    card = EXITS.upsert(
        ExitProposal(
            position_id=pos.id,
            symbol="AAPL",
            action=TradeAction.SELL,
            entry=pos.avg_entry,
            current=pos.avg_entry,
            pnl_pct=0.0,
            reasons=[OPERATOR_CLOSE_REASON],
            recommendation=UserDecision.SELL,
            confidence=1.0,
        )
    )

    _withdraw_unsupported(evaluated={"AAPL"}, proposed=set())

    still_there = EXITS.get(card.id)
    assert still_there is not None
    assert still_there.status == EXIT_AWAITING

@pytest.mark.asyncio
async def test_a_halted_desk_can_still_be_flattened() -> None:
    """The kill switch refuses new exposure, never the shedding of it."""
    service, _ = await _service_with_open_position()
    set_kill_switch(True)

    result = await service.close_position("AAPL")

    assert result.status == EXIT_SOLD
