"""Paying up to cross the offer may not quietly change which trade this is.

Approval reprices the entry to the live offer. The stop and the target stay
where the strategy drew them, so every cent paid above the card lengthens the
risk and shortens the reward. On 2026-08-31 OXY was drawn at 59.11 for 2:1,
filled at 59.97, and became a 0.32:1 trade — $128 of risk buying $40 of reward
— while the card on screen still read 2.0. `PRICE_MOVED_PAST_SETUP` let it
through because the price had not passed the target, only most of the way.

Two properties are pinned here. The gate refuses a setup that no longer clears
the strategy's own bar, and the ledger records the trade that happened rather
than the one that was proposed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agents.strategy.agent import MIN_RISK_REWARD
from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OpportunityStatus, TradeAction, TradingMode, UserDecision
from core.schemas import Quote, TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, LiquidMarketData
from trading.execution import MAX_ENTRY_SLIPPAGE_R, ExecutionService
from trading.exits import MemoryExitStore
from trading.ledger import LEDGER
from trading.opportunities import MemoryOpportunityStore

# A 2:1 card: risk 1.00, reward 2.00.
CARD_ENTRY = Decimal("100.00")
CARD_STOP = Decimal("99.00")
CARD_TARGET = Decimal("102.00")


class BookAt(LiquidMarketData):
    """A market whose offer sits wherever the test needs it."""

    def __init__(self, ask: float) -> None:
        super().__init__(price=ask)
        self.ask_price = Decimal(str(ask))

    async def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            bid=self.ask_price - Decimal("0.01"),
            ask=self.ask_price,
            ts=self._now(),
            source="synthetic",
        )


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=CARD_ENTRY,
        stop=CARD_STOP,
        target=CARD_TARGET,
        risk_reward=2.0,
        reasons=["test setup"],
        strategy_version="test@1",
    )


async def _approve(broker: MockPaperBroker, *, ask: float):
    store = MemoryOpportunityStore()
    card = _candidate()
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=BookAt(ask),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )
    return await service.decide(opp.id, UserDecision.APPROVE)


@pytest.fixture(autouse=True)
def _armed_desk():
    set_kill_switch(False)
    yield
    set_kill_switch(False)


def test_the_allowance_cannot_be_widened_past_the_strategy_bar() -> None:
    """The bound on the entry and the strategy's target geometry are linked.

    The strategy builds its target at `MIN_RISK_REWARD` times risk, so the
    admissible slippage and the worst admissible trade are two views of one
    number. Pinned together here: raising the allowance silently lowers the
    quality of trade the desk will accept, and this is where that shows up.
    """
    worst = (MIN_RISK_REWARD - MAX_ENTRY_SLIPPAGE_R) / (1 + MAX_ENTRY_SLIPPAGE_R)
    assert worst == pytest.approx(1.4)


@pytest.mark.asyncio
async def test_an_entry_that_no_longer_pays_for_its_risk_is_refused() -> None:
    """The OXY shape: inside the target, far past the point of being worth it.

    The card risks 1.00 and an offer at 101.30 asks 1.30 to enter — more than
    the whole distance being risked, for a trade that reads 2:1 on screen.
    """
    broker = MockPaperBroker()

    with pytest.raises(RuntimeError, match="ENTRY_TOO_FAR_ABOVE_CARD"):
        await _approve(broker, ask=101.30)

    assert broker.orders == []


@pytest.mark.asyncio
async def test_the_live_oxy_entry_would_now_be_refused() -> None:
    """The exact numbers the desk took on 2026-08-31.

    Card at 59.1072 against a stop at 58.4328, filled at 59.97 — 0.86 paid to
    enter on 0.67 of planned risk.
    """
    card = TradeCandidate(
        symbol="OXY",
        action=TradeAction.BUY,
        confidence=0.7,
        entry=Decimal("59.1072"),
        stop=Decimal("58.4328"),
        target=Decimal("60.456"),
        risk_reward=2.0,
        reasons=["live card"],
        strategy_version="strategy_confluence@0.2.0",
    )
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        market_data=BookAt(59.96),
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
    )

    with pytest.raises(RuntimeError, match="ENTRY_TOO_FAR_ABOVE_CARD"):
        await service.decide(opp.id, UserDecision.APPROVE)

    assert broker.orders == []


@pytest.mark.asyncio
async def test_an_entry_that_still_pays_for_its_risk_is_taken() -> None:
    """Crossing the spread is not itself the problem; crossing too far is."""
    broker = MockPaperBroker()

    result = await _approve(broker, ask=100.00)

    assert result.status is OpportunityStatus.EXECUTED
    assert len(broker.orders) >= 1


@pytest.mark.asyncio
async def test_the_ledger_records_the_trade_that_happened() -> None:
    """Not the one on the card.

    Every position opened on 2026-08-31 recorded `risk_reward: 2.0`, having
    been taken at 2.04, 1.97, 1.53 and 0.32. The journal is the evidence the
    strategy is later judged by, so it may not carry the proposal's number.
    """
    broker = MockPaperBroker()
    await _approve(broker, ask=100.00)

    row = LEDGER.find_open_by_symbol("AAPL")
    assert row is not None
    payload = row.payload or {}

    fill = Decimal(str(row.avg_entry))
    target = Decimal(str(row.target_price))
    expected = round(float((target - fill) / (fill - CARD_STOP)), 2)
    assert payload["risk_reward"] == expected
    assert payload["card_risk_reward"] >= 2.0
