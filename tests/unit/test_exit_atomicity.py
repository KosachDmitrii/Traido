"""P0-3: one fill reduces the position exactly once, whatever crashes.

`apply_exit_to_ledger` is the single place where a broker fill becomes a smaller
position, and two callers reach it for the same fill as a matter of routine: the
exit path applies it, then a reconciliation pass reads the same filled order off
the broker and applies it again. The bookkeeping that makes the second call a
no-op is `applied_exit_qty`, so the order in which it is written relative to the
ledger is not an implementation detail — it decides which way a crash falls.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from core.enums import IntentPurpose, IntentStatus, OrderSide, OrderType
from trading.intents import MemoryOrderIntentStore, apply_exit_to_ledger
from trading.ledger import LEDGER
from trading.order_intent import OrderIntent


@pytest.fixture
def position(isolated_ledger):
    from core.enums import TradeAction, TradingMode
    from core.schemas import PortfolioSnapshot, TradeCandidate
    from risk.risk_engine import RiskEngine
    from tests.support import CLEARED_EARNINGS
    from trading.opportunities import MemoryOpportunityStore

    candidate = TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(115),
        risk_reward=3.0,
        reasons=["exit atomicity"],
        strategy_version="test@1",
    )
    snapshot = PortfolioSnapshot(
        equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
    )
    opp = MemoryOpportunityStore().create(
        candidate,
        RiskEngine().evaluate(candidate, snapshot, context=CLEARED_EARNINGS),
        TradingMode.CONFIRMATION,
    )
    return LEDGER.open_from_opportunity(
        opp,
        qty=Decimal(100),
        broker_entry_order_id="entry-1",
        fill_price=Decimal(100),
        stop_order_id="stop-1",
    )


def _exit_intent(position_id) -> tuple[MemoryOrderIntentStore, OrderIntent]:
    store = MemoryOrderIntentStore()
    intent, _ = store.create_or_get(
        OrderIntent(
            idempotency_key=f"exit:{position_id}:0",
            purpose=IntentPurpose.EXIT,
            broker="Fake",
            symbol="AAPL",
            side=OrderSide.SELL,
            requested_qty=Decimal(40),
            order_type=OrderType.MARKET,
            position_id=position_id,
        )
    )
    store.transition(intent.id, IntentStatus.SUBMITTING, client_order_id="c-1")
    return store, store.get(intent.id)


def test_the_same_fill_seen_twice_reduces_the_position_once(position) -> None:
    """The ordinary case: the exit path and a reconciliation pass both see it."""
    store, intent = _exit_intent(position.id)

    apply_exit_to_ledger(
        store, intent, filled_qty=Decimal(40), exit_price=Decimal(110), reasons=["target"]
    )
    apply_exit_to_ledger(
        store,
        store.get(intent.id),
        filled_qty=Decimal(40),
        exit_price=Decimal(110),
        reasons=["target"],
    )

    assert Decimal(str(LEDGER.get(position.id).qty)) == Decimal(60)


def test_a_crash_between_the_two_writes_does_not_reduce_twice(position, monkeypatch) -> None:
    """The window that made this a P0.

    The ledger write and the `applied_exit_qty` write are separate commits. If
    the process dies between them and the fill is replayed — which reconciliation
    does by design — the old order reduced the position a second time for one
    fill. The position closes on paper while the shares are still held, no
    mismatch is raised because a flat book has nothing to compare, and the stop
    is cancelled off a position that still exists.
    """
    store, intent = _exit_intent(position.id)

    original = LEDGER.apply_exit_fill

    def _die_after_the_ledger_write(**kwargs):
        original(**kwargs)
        raise RuntimeError("process died before the bookkeeping was written")

    monkeypatch.setattr(LEDGER, "apply_exit_fill", _die_after_the_ledger_write)
    with pytest.raises(RuntimeError):
        apply_exit_to_ledger(
            store, intent, filled_qty=Decimal(40), exit_price=Decimal(110), reasons=["target"]
        )

    monkeypatch.setattr(LEDGER, "apply_exit_fill", original)
    apply_exit_to_ledger(
        store,
        store.get(intent.id),
        filled_qty=Decimal(40),
        exit_price=Decimal(110),
        reasons=["target"],
    )

    assert Decimal(str(LEDGER.get(position.id).qty)) == Decimal(60), (
        "one fill of 40 against 100 leaves 60, however many times it is replayed"
    )


def test_a_crash_before_the_ledger_write_loses_a_reduction_rather_than_duplicating_one(
    position, monkeypatch
) -> None:
    """The failure the fix chooses, stated so it cannot be mistaken for a bug.

    Claiming first means the surviving hazard is a book that is *larger* than the
    venue. That is the direction reconciliation can see: it refuses to absorb an
    unexplained shrink, blocks the symbol, and the excess-protection sweep stops
    the stop covering shares that are gone. The opposite error is silent.
    """
    store, intent = _exit_intent(position.id)

    def _die_before_the_ledger_write(**_kwargs):
        raise RuntimeError("process died after claiming, before reducing")

    monkeypatch.setattr(LEDGER, "apply_exit_fill", _die_before_the_ledger_write)
    with pytest.raises(RuntimeError):
        apply_exit_to_ledger(
            store, intent, filled_qty=Decimal(40), exit_price=Decimal(110), reasons=["target"]
        )

    assert Decimal(str(LEDGER.get(position.id).qty)) == Decimal(100)
    assert store.get(intent.id).applied_exit_qty == Decimal(40), (
        "the claim is durable, so the replay will not reduce twice either"
    )


def test_a_larger_second_fill_applies_only_the_increment(position) -> None:
    """A partial exit that fills further must move the book by the difference."""
    store, intent = _exit_intent(position.id)

    apply_exit_to_ledger(
        store, intent, filled_qty=Decimal(10), exit_price=Decimal(110), reasons=["partial"]
    )
    apply_exit_to_ledger(
        store,
        store.get(intent.id),
        filled_qty=Decimal(25),
        exit_price=Decimal(110),
        reasons=["partial"],
    )

    assert Decimal(str(LEDGER.get(position.id).qty)) == Decimal(75)
    assert store.get(intent.id).applied_exit_qty == Decimal(25)


def test_two_readers_racing_on_one_fill_yield_one_reduction(position) -> None:
    """Both hold the same pre-claim snapshot, as two concurrent passes would."""
    store, intent = _exit_intent(position.id)
    snapshot = store.get(intent.id)

    apply_exit_to_ledger(
        store, snapshot, filled_qty=Decimal(40), exit_price=Decimal(110), reasons=["target"]
    )
    apply_exit_to_ledger(
        store, snapshot, filled_qty=Decimal(40), exit_price=Decimal(110), reasons=["target"]
    )

    assert Decimal(str(LEDGER.get(position.id).qty)) == Decimal(60)


def test_the_claim_is_refused_when_the_expected_quantity_has_moved() -> None:
    store = MemoryOrderIntentStore()
    intent, _ = store.create_or_get(
        OrderIntent(
            idempotency_key=f"exit:{uuid4()}:0",
            purpose=IntentPurpose.EXIT,
            broker="Fake",
            symbol="AAPL",
            side=OrderSide.SELL,
            requested_qty=Decimal(10),
            order_type=OrderType.MARKET,
        )
    )

    assert store.claim_exit_qty(intent.id, expect=Decimal(0), claim=Decimal(10)) is True
    assert store.claim_exit_qty(intent.id, expect=Decimal(0), claim=Decimal(10)) is False
