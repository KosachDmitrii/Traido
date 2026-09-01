"""US session helpers + stale approving recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from core.enums import OpportunityStatus, TradeAction, TradingMode
from core.schemas import PortfolioSnapshot, TradeCandidate, TradeOpportunity
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS
from trading.opportunities import MemoryOpportunityStore
from trading.session_hours import fill_wait_seconds, us_equity_rth_open


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(110),
        risk_reward=2.0,
        reasons=["t"],
        strategy_version="strategy_confluence@0.2.0",
        pipeline_run_id=uuid4(),
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(100000),
        cash=Decimal(100000),
        buying_power=Decimal(100000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )


def test_fill_wait_shorter_outside_session() -> None:
    assert fill_wait_seconds(in_session=True) == 18.0
    assert fill_wait_seconds(in_session=False) == 2.5


def test_us_equity_weekend_closed() -> None:
    sat = datetime(2026, 8, 29, 19, 0, tzinfo=UTC)
    assert us_equity_rth_open(sat) is False


def test_release_legacy_approving_without_claimed_at() -> None:
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(_candidate(), _portfolio(), context=CLEARED_EARNINGS)
    opp = TradeOpportunity(
        id=uuid4(),
        candidate=_candidate(),
        risk=risk,
        status=OpportunityStatus.APPROVING,
        trading_mode=TradingMode.CONFIRMATION,
        created_at=datetime.now(UTC),
        claimed_at=None,
    )
    store.update(opp)
    n = store.release_stale_approving(older_than_sec=90.0)
    assert n == 1
    assert store.get(opp.id).status == OpportunityStatus.AWAITING_CONFIRMATION


def test_fresh_claim_not_released() -> None:
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(_candidate(), _portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    claimed = store.claim(
        opp.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.APPROVING,
    )
    assert claimed is not None
    assert claimed.claimed_at is not None
    n = store.release_stale_approving(older_than_sec=90.0)
    assert n == 0
    assert store.get(opp.id).status == OpportunityStatus.APPROVING
