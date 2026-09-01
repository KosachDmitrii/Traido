"""A BUY card is an offer to act, and an offer that cannot be taken is a lie.

Two ways a card dies on the desk: its hour runs out, or the symbol gains a
position while it waits. Both were invisible until the operator clicked and was
refused, and both hold one of the five queue slots that stop the scanner when
full. What must *not* happen is a sweep so eager it deletes good setups: a wide
spread and a shut market refuse an entry too, and both come back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.enums import (
    OpportunityStatus,
    RiskVerdict,
    TradeAction,
    TradingMode,
)
from core.schemas import PortfolioSnapshot, RiskDecision, TradeCandidate
from risk.limits import default_risk_limits
from trading.opportunities import MemoryOpportunityStore, withdraw_unactionable


def _candidate(symbol: str) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(98),
        target=Decimal(104),
        risk_reward=2.0,
        confidence=0.7,
        reasons=["pullback"],
        strategy_version="test",
    )


def _risk() -> RiskDecision:
    return RiskDecision(
        candidate_id=uuid4(),
        verdict=RiskVerdict.PASS,
        sized_qty=Decimal(10),
        limits_applied=default_risk_limits(),
        portfolio=PortfolioSnapshot(
            equity=Decimal(100000),
            cash=Decimal(100000),
            buying_power=Decimal(100000),
            open_exposure=Decimal(0),
            day_pnl=Decimal(0),
            week_pnl=Decimal(0),
            drawdown_pct=0.0,
            open_positions=0,
        ),
    )


def _card(store: MemoryOpportunityStore, symbol: str, *, age_minutes: int = 0):
    opp = store.create(_candidate(symbol), _risk(), TradingMode.CONFIRMATION)
    if age_minutes:
        born = datetime.now(UTC) - timedelta(minutes=age_minutes)
        store.update(
            opp.model_copy(update={"created_at": born, "expires_at": born + timedelta(minutes=60)})
        )
    return store.get(opp.id)


@pytest.fixture
def held(monkeypatch):
    """Control what the book claims to hold, without touching the real journal."""
    holdings: set[str] = set()

    class _Ledger:
        @staticmethod
        def find_open_by_symbol(symbol: str):
            return object() if symbol.upper() in holdings else None

    import trading.ledger

    monkeypatch.setattr(trading.ledger, "LEDGER", _Ledger())
    return holdings


def test_a_card_past_its_hour_is_taken_down(held):
    store = MemoryOpportunityStore()
    old = _card(store, "AAA", age_minutes=61)
    assert old is not None

    assert withdraw_unactionable(store) == 1

    assert store.get(old.id).status is OpportunityStatus.EXPIRED
    assert store.list_open() == []


def test_a_card_still_inside_its_hour_is_left_alone(held):
    store = MemoryOpportunityStore()
    fresh = _card(store, "AAA", age_minutes=59)
    assert fresh is not None

    assert withdraw_unactionable(store) == 0
    assert store.get(fresh.id).status is OpportunityStatus.AWAITING_CONFIRMATION


def test_a_card_for_a_symbol_now_held_is_taken_down(held):
    """The exact state the desk was found in: three cards, three open positions."""
    store = MemoryOpportunityStore()
    doomed = _card(store, "MO")
    live = _card(store, "ADBE")
    held.add("MO")

    assert withdraw_unactionable(store) == 1

    assert store.get(doomed.id).status is OpportunityStatus.DISCARDED
    assert store.get(live.id).status is OpportunityStatus.AWAITING_CONFIRMATION
    assert [o.candidate.symbol for o in store.list_open()] == ["ADBE"]


def test_a_card_the_operator_is_already_pressing_is_not_yanked(held):
    """`claim` decides the race, and a card mid-approval has already won it."""
    store = MemoryOpportunityStore()
    pressed = _card(store, "MO", age_minutes=61)
    assert pressed is not None
    store.claim(
        pressed.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.APPROVING,
    )
    held.add("MO")

    assert withdraw_unactionable(store) == 0
    assert store.get(pressed.id).status is OpportunityStatus.APPROVING


def test_the_sweep_is_idempotent(held):
    store = MemoryOpportunityStore()
    _card(store, "AAA", age_minutes=61)

    assert withdraw_unactionable(store) == 1
    assert withdraw_unactionable(store) == 0


async def test_a_held_symbol_is_never_analysed_for_an_entry(held, monkeypatch):
    """The cheapest refusal is the one taken before the expensive part runs.

    One position per symbol was enforced only at the click, so a name the book
    already held still cost a full pass — supervisor, LLM calls and all — to
    produce a card that `POSITION_ALREADY_OPEN` was always going to refuse.
    """
    from trading import pipeline

    def _explode(*args, **kwargs):
        raise AssertionError("a held symbol must not reach the supervisor")

    monkeypatch.setattr(pipeline, "build_supervisor", _explode)
    monkeypatch.setattr(pipeline, "open_scan_context", _explode)
    held.add("MO")

    result = await pipeline.run_symbol_pipeline("MO")

    assert result.status == "position_open"
    assert result.candidate is None


def test_a_symbol_we_never_looked_at_is_not_filed_as_having_no_setup():
    """`no candidate` means we looked and found nothing. This is not that."""
    from agents.scanner.cycle import _record_deep_outcome
    from agents.scanner.funnel import ScanFunnel
    from core.schemas import PipelineResult

    funnel = ScanFunnel()
    funnel.universe_total = 1
    _record_deep_outcome(
        PipelineResult(pipeline_run_id=uuid4(), symbol="MO", status="position_open"),
        funnel,
        [],
    )

    assert funnel.position_open == 1
    assert funnel.deep_analysis_no_candidate == 0
    assert funnel.deep_analysis_failed == 0
    assert funnel.reconciles(), "the one symbol has to land in exactly one bucket"


def test_nothing_temporary_takes_a_card_down(held):
    """A live setup survives everything that can come back.

    Spread, session and price are all read at the click, and all of them recover
    on their own. Sweeping on them would delete a good idea for being briefly
    unbuyable — which is why the sweep is deliberately blind to them.
    """
    store = MemoryOpportunityStore()
    good = _card(store, "ADBE", age_minutes=5)
    assert good is not None

    assert withdraw_unactionable(store) == 0
    assert store.get(good.id).status is OpportunityStatus.AWAITING_CONFIRMATION
