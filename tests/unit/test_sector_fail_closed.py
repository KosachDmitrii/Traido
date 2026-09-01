"""An unclassified sector is not an empty sector — it is a refused entry.

`sector_of` answers `"unknown"` for a name outside `configs/universe.json`.
Treating that string as a sector bucket let a technology name skip a full
technology cap: the candidate landed in an empty `"unknown"` bucket while the
real sector sat at 24%. Silence is not clearance; it is `SECTOR_UNCLASSIFIED`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.enums import EarningsCheck, NewsCheck, SectorCheck, TradeAction
from core.schemas import PortfolioSnapshot, RiskLimits, TradeCandidate
from risk.risk_engine import RiskContext, RiskEngine

_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def _candidate(symbol: str = "PLTR") -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(98),
        target=Decimal(104),
        confidence=0.8,
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )


def test_an_unclassified_name_is_refused() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector=None,
        sector_check=SectorCheck.UNCLASSIFIED,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)
    assert "SECTOR_UNCLASSIFIED" in decision.reasons


def test_a_caller_that_never_looked_is_refused_as_unverified() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)
    assert "SECTOR_UNVERIFIED" in decision.reasons


def test_missing_key_and_vendor_gap_are_named_apart() -> None:
    for status, code in (
        (SectorCheck.NOT_CONFIGURED, "SECTOR_NOT_CONFIGURED"),
        (SectorCheck.UNAVAILABLE, "SECTOR_UNAVAILABLE"),
    ):
        ctx = RiskContext(
            news=NewsCheck.CHECKED,
            earnings=EarningsCheck.CHECKED,
            sector_check=status,
            now=_NOW,
        )
        decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)
        assert code in decision.reasons


def test_unclassified_book_exposure_counts_against_the_candidate_sector() -> None:
    """Shares we cannot place are charged to the sector we are about to add to."""
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector="technology",
        sector_check=SectorCheck.CHECKED,
        sector_exposure={"technology": Decimal(0)},
        unclassified_exposure=Decimal(24_000),
        now=_NOW,
    )
    # Equity 100k, adding ~sized notional on top of 24k unclassified → over 25%.
    decision = RiskEngine(RiskLimits(max_sector_pct=25.0)).evaluate(
        _candidate("AAPL"), _portfolio(), context=ctx
    )
    assert "MAX_SECTOR_EXPOSURE" in decision.reasons


def test_the_waiver_is_recorded_when_sector_check_is_off() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.UNCLASSIFIED,
        now=_NOW,
    )
    decision = RiskEngine(RiskLimits(require_sector_check=False)).evaluate(
        _candidate(), _portfolio(), context=ctx
    )
    assert "SECTOR_UNCLASSIFIED" not in decision.reasons
    assert decision.limits_applied.require_sector_check is False
