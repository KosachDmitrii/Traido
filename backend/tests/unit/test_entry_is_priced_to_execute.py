"""An approved entry is priced to execute, not to wait.

pytestmark = pytest.mark.usefixtures("capital_path_ready")

The strategy proposes `entry = min(SMA20, close)` — the level it would like to
buy a pullback at — and it requires an uptrend, where SMA20 sits below price. So
the card's entry is at or below the last close by construction. Execution gives
a limit eighteen seconds before cancelling it, which is not long enough for a
market to come down to meet it.

Run live on 2026-08-31 the two halves met exactly as that predicts: a card for
MO at 66.47, 0.2% under the market, timed out unfilled with a clean cancel. The
order now crosses the live offer instead, with a bounded buffer, and is re-sized
by the risk engine at the price it will actually pay.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OrderType, TradeAction, TradingMode, UserDecision
from core.schemas import TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ENTRY_BUFFER_BPS, ExecutionService
from trading.exits import MemoryExitStore
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]

CARD_ENTRY = Decimal("66.47")
CARD_STOP = Decimal("65.9364")
CARD_TARGET = Decimal("67.5372")

def _card() -> TradeCandidate:
    return TradeCandidate(
        symbol="MO",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=CARD_ENTRY,
        stop=CARD_STOP,
        target=CARD_TARGET,
        risk_reward=2.0,
        reasons=["pullback entry"],
        strategy_version="strategy_confluence@0.2.0",
        pipeline_run_id=uuid4(),
    )

async def _approve(broker: MockPaperBroker, *, market_price: float):
    store = MemoryOpportunityStore()
    card = _card()
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(price=market_price),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )
    return risk, await service.decide(opp.id, UserDecision.APPROVE, request_id=uuid4(), expected_decision_version=opp.decision_version)

def _entry_order(broker: MockPaperBroker):
    return next(o for o in broker.orders if o.order_type is OrderType.LIMIT)

def _ask(market_price: float) -> Decimal:
    """`liquid_market_data` quotes one basis point either side of the mid."""
    price = Decimal(str(market_price))
    return price + price * Decimal("0.0001")

@pytest.mark.asyncio
async def test_the_entry_crosses_the_offer_rather_than_resting_below_it() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()

    await _approve(broker, market_price=66.47)

    entry = _entry_order(broker)
    assert entry.limit_price is not None
    assert entry.limit_price >= _ask(66.47), "the limit does not reach the offer"
    assert entry.limit_price > CARD_ENTRY, "the order still rests at the card's pullback level"

@pytest.mark.asyncio
async def test_the_buffer_above_the_offer_is_bounded() -> None:
    """The reason this is a limit and not a market order."""
    set_kill_switch(False)
    broker = MockPaperBroker()

    await _approve(broker, market_price=66.47)

    ceiling = _ask(66.47) * (Decimal(1) + Decimal(str(ENTRY_BUFFER_BPS)) / Decimal(10_000))
    entry = _entry_order(broker)
    assert entry.limit_price is not None
    assert entry.limit_price <= ceiling.quantize(Decimal("0.01")) + Decimal("0.01")

@pytest.mark.asyncio
async def test_paying_up_buys_fewer_shares() -> None:
    """The re-check may only move one way.

    The stop does not move with the price, so an entry above the card's is an
    entry with more risk per share. Sizing is re-derived by the engine at the
    price we will pay, so the position shrinks rather than the risk growing.
    """
    set_kill_switch(False)
    broker = MockPaperBroker()

    # The market ran up between the scan and the click, but stayed inside the
    # slippage allowance — past it the entry is refused rather than resized,
    # which `test_repricing_preserves_the_setup.py` covers.
    card_risk, _ = await _approve(broker, market_price=66.52)

    entry = _entry_order(broker)
    assert card_risk.sized_qty is not None
    assert entry.qty < card_risk.sized_qty, (
        f"paid {entry.limit_price} against a card at {CARD_ENTRY} "
        f"but still bought {entry.qty} shares"
    )

@pytest.mark.asyncio
async def test_the_stop_does_not_move_with_the_entry() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()

    await _approve(broker, market_price=66.47)

    stop = next(o for o in broker.orders if o.order_type is OrderType.STOP)
    assert stop.stop_price == CARD_STOP.quantize(Decimal("0.01"))

@pytest.mark.asyncio
async def test_a_market_that_ran_past_the_target_is_refused() -> None:
    """Buying above the target is not the trade the card described."""
    set_kill_switch(False)
    broker = MockPaperBroker()

    with pytest.raises(RuntimeError, match="PRICE_MOVED_PAST_SETUP"):
        await _approve(broker, market_price=float(CARD_TARGET) + 1.0)

    assert not broker.orders, "an entry above its own target reached the broker"
