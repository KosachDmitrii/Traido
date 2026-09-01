"""Every order we originate is a whole number of shares.

Sizing is a division, so it produces a fraction — 5% of a $100k book at $66.47
is 75.2219 shares. Alpaca accepts a fractional quantity only with
`time_in_force=day`, and a protective stop is sent GTC because it has to outlive
the session. A fractional entry therefore buys a position whose stop the venue
refuses, and the only way out of an unprotectable position is an emergency
close: the desk's first live trade would have been a round trip that proved the
backstop rather than the trade.

The fraction is dropped at the point sizing enters execution, so protection and
exits inherit a whole share count without any of them having to know why.
"""

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
from trading.exits import MemoryExitStore
from trading.opportunities import MemoryOpportunityStore


def _fractionally_sized_candidate() -> TradeCandidate:
    """Priced so the position cap divides into a fraction, as MO did live."""
    return TradeCandidate(
        symbol="MO",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal("66.47"),
        stop=Decimal("65.9364"),
        target=Decimal("67.5372"),
        risk_reward=2.0,
        reasons=["whole-share sizing"],
        strategy_version="strategy_confluence@0.2.0",
        pipeline_run_id=uuid4(),
    )


async def _approve(broker: MockPaperBroker, candidate: TradeCandidate, *, price: float = 66.47):
    """Approve at a live book quoted around `price`.

    The book has to match the candidate: the entry is now priced off the offer,
    so a $9,000 setup quoted at $66 is not an expensive share, it is a share
    whose stop sits far above the market.
    """
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=liquid_market_data(price=price),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )
    return risk, await service.decide(opp.id, UserDecision.APPROVE)


@pytest.mark.asyncio
async def test_sizing_produces_a_fraction_that_never_reaches_the_broker() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    candidate = _fractionally_sized_candidate()

    risk, result = await _approve(broker, candidate)

    # The premise: without rounding there would be nothing to fix.
    assert risk.sized_qty is not None
    assert risk.sized_qty != risk.sized_qty.to_integral_value()

    assert result.status == OpportunityStatus.EXECUTED
    assert broker.orders, "the entry never reached the broker"
    for order in broker.orders:
        assert order.qty == order.qty.to_integral_value(), (
            f"{order.order_type.value} order for {order.qty} shares is fractional; "
            "a GTC stop of that size is refused by the venue"
        )


@pytest.mark.asyncio
async def test_the_protective_stop_is_whole_shares_too() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()

    await _approve(broker, _fractionally_sized_candidate())

    stops = [o for o in broker.orders if o.order_type.value == "stop"]
    assert stops, "the position was left without a protective stop"
    for stop in stops:
        assert stop.qty == stop.qty.to_integral_value()


@pytest.mark.asyncio
async def test_a_position_under_one_share_is_refused_not_sent_as_zero() -> None:
    """Rounding down can reach zero, and a zero-quantity order is not an entry.

    Refusing here keeps the failure a named rejection rather than a broker
    error on a nonsensical order.
    """
    set_kill_switch(False)
    broker = MockPaperBroker()
    candidate = _fractionally_sized_candidate().model_copy(
        # A share price above the whole position cap: 5% of $100k cannot buy one.
        update={
            "entry": Decimal("9000.00"),
            "stop": Decimal("8900.00"),
            "target": Decimal("9200.00"),
        }
    )

    with pytest.raises(RuntimeError, match="SIZE_BELOW_ONE_SHARE"):
        await _approve(broker, candidate, price=9000.0)

    assert not broker.orders, "a sub-share entry still reached the broker"
