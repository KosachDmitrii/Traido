"""Correlation, sector, position-count, and event-risk gates in the Risk Engine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from core.enums import EarningsCheck, NewsCheck, RiskVerdict, SectorCheck, TradeAction
from core.schemas import PortfolioSnapshot, RiskLimits, TradeCandidate
from quant.correlation import build_correlation_matrix
from risk.limits import load_risk_limits
from risk.risk_engine import RiskContext, RiskEngine
from tests.support import CLEARED_EARNINGS

_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
_TODAY = _NOW.date()


def _candidate(symbol: str = "NVDA") -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(115),
        confidence=0.8,
        risk_reward=3.0,
        reasons=["test"],
        strategy_version="test@1",
    )


def _portfolio(**over) -> PortfolioSnapshot:  # type: ignore[no-untyped-def]
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
    return PortfolioSnapshot(**base)  # type: ignore[arg-type]


def _correlated_matrix(*symbols: str):  # type: ignore[no-untyped-def]
    r = [0.01, -0.02, 0.03, -0.01, 0.015] * 8
    return build_correlation_matrix({s: r for s in symbols})


def _uncorrelated_matrix():  # type: ignore[no-untyped-def]
    a = [0.01, -0.02, 0.03, -0.01, 0.015] * 8
    b = [-0.01, 0.025, 0.005, 0.02, -0.03] * 8
    return build_correlation_matrix({"NVDA": a, "KO": b})


# ── What silence means ───────────────────────────────────────────────────────


def test_a_caller_that_supplies_no_context_is_refused_the_entry() -> None:
    """Not a regression — the point.

    An engine handed nothing has verified nothing. Two inputs cannot be shrugged
    off: a stop does not survive an earnings print, and the strategy's veto on
    bad headlines is only a veto while the feed behind it was read. Passing here
    would mean every caller who forgets to fetch either is silently promoted to
    one who cleared it.
    """
    decision = RiskEngine().evaluate(_candidate(), _portfolio())
    assert decision.verdict is RiskVerdict.REJECT
    assert decision.reasons == [
        "EARNINGS_UNVERIFIED",
        "NEWS_UNVERIFIED",
        "SECTOR_UNVERIFIED",
        "REGIME_MISSING",
    ]


def test_missing_correlation_data_is_skipped_rather_than_refused() -> None:
    """The other direction, and deliberately so.

    A book we cannot see is not evidence of concentration, so an absent
    correlation matrix skips that check. An absent calendar is not evidence of
    an absent print, so it does not skip that one. The asymmetry is the design.
    """
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=CLEARED_EARNINGS)
    assert decision.verdict is RiskVerdict.PASS
    assert decision.reasons == ["RISK_OK"]


# ── Correlation ──────────────────────────────────────────────────────────────


def test_duplicate_sector_exposure_is_rejected() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        open_symbols=["AMD"],
        correlations=_correlated_matrix("NVDA", "AMD"),
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate("NVDA"), _portfolio(), context=ctx)
    assert decision.verdict is RiskVerdict.REJECT
    assert "MAX_CORRELATION" in decision.reasons


def test_uncorrelated_addition_is_allowed() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        open_symbols=["KO"],
        correlations=_uncorrelated_matrix(),
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate("NVDA"), _portfolio(), context=ctx)
    assert decision.verdict is RiskVerdict.PASS


def test_correlation_threshold_is_configurable() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        open_symbols=["AMD"],
        correlations=_correlated_matrix("NVDA", "AMD"),
        regime_tradable=True,
        now=_NOW,
    )
    permissive = RiskEngine(RiskLimits(max_correlation=1.0))
    assert permissive.evaluate(_candidate(), _portfolio(), context=ctx).verdict is RiskVerdict.PASS


def test_book_of_clones_trips_diversification_check() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        open_symbols=["AMD", "INTC", "AVGO"],
        correlations=_correlated_matrix("NVDA", "AMD", "INTC", "AVGO"),
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine(RiskLimits(max_correlation=1.0)).evaluate(
        _candidate(), _portfolio(), context=ctx
    )
    assert "INSUFFICIENT_DIVERSIFICATION" in decision.reasons


# ── Event risk ───────────────────────────────────────────────────────────────


def test_earnings_tomorrow_blocks_the_trade() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        next_earnings=_TODAY + timedelta(days=1),
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)
    assert "EARNINGS_IMMINENT" in decision.reasons


def test_earnings_far_away_does_not_block() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        next_earnings=_TODAY + timedelta(days=45),
        regime_tradable=True,
        now=_NOW,
    )
    assert (
        RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx).verdict is RiskVerdict.PASS
    )


def test_just_reported_earnings_blocks_the_trade() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED, earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED, last_earnings=_TODAY, now=_NOW
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)
    assert "EARNINGS_JUST_REPORTED" in decision.reasons


def test_earnings_window_is_configurable() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        next_earnings=_TODAY + timedelta(days=10),
        regime_tradable=True,
        now=_NOW,
    )
    wide = RiskEngine(RiskLimits(block_days_before_earnings=14))
    assert "EARNINGS_IMMINENT" in wide.evaluate(_candidate(), _portfolio(), context=ctx).reasons


def test_zero_day_window_still_blocks_same_day_earnings() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED, earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED, next_earnings=_TODAY, now=_NOW
    )
    engine = RiskEngine(RiskLimits(block_days_before_earnings=0))
    assert "EARNINGS_IMMINENT" in engine.evaluate(_candidate(), _portfolio(), context=ctx).reasons


def test_past_earnings_date_in_next_field_is_ignored() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        next_earnings=date(2020, 1, 1),
        regime_tradable=True,
        now=_NOW,
    )
    assert (
        RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx).verdict is RiskVerdict.PASS
    )


# ── Regime and position count ────────────────────────────────────────────────


def test_untradable_regime_blocks_new_longs() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED, earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED, regime_tradable=False, now=_NOW
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)
    assert "REGIME_NOT_TRADABLE" in decision.reasons


def test_full_book_blocks_new_positions() -> None:
    engine = RiskEngine(RiskLimits(max_open_positions=3))
    decision = engine.evaluate(_candidate(), _portfolio(open_positions=3))
    assert "MAX_OPEN_POSITIONS" in decision.reasons


# ── Sector concentration ─────────────────────────────────────────────────────


def test_sector_cap_blocks_an_overweight_sector() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        sector="technology",
        sector_exposure={"technology": Decimal(24000)},
        regime_tradable=True,
        now=_NOW,
    )
    engine = RiskEngine(RiskLimits(max_sector_pct=25.0))
    decision = engine.evaluate(_candidate(), _portfolio(), context=ctx)
    assert "MAX_SECTOR_EXPOSURE" in decision.reasons


def test_sector_cap_allows_room_in_an_empty_sector() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        sector="staples",
        sector_exposure={},
        regime_tradable=True,
        now=_NOW,
    )
    assert (
        RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx).verdict is RiskVerdict.PASS
    )


# ── Precedence ───────────────────────────────────────────────────────────────


def test_kill_switch_short_circuits_every_other_check() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        next_earnings=_TODAY,
        regime_tradable=False,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(kill_switch=True), context=ctx)
    assert decision.reasons == ["KILL_SWITCH"]


def test_all_breaches_are_reported_together() -> None:
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        open_symbols=["AMD"],
        correlations=_correlated_matrix("NVDA", "AMD"),
        next_earnings=_TODAY,
        regime_tradable=False,
        now=_NOW,
    )
    reasons = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx).reasons
    assert {"MAX_CORRELATION", "EARNINGS_IMMINENT", "REGIME_NOT_TRADABLE"} <= set(reasons)


# ── Config loading ───────────────────────────────────────────────────────────


def test_limits_load_from_the_locked_config() -> None:
    limits = load_risk_limits()
    assert limits.max_risk_per_trade_pct == 1.0
    assert limits.max_position_pct == 5.0
    assert limits.max_daily_loss_pct == 2.0
    assert limits.allow_leverage is False
    assert limits.allow_short is False
    assert limits.allow_options is False


def test_missing_config_falls_back_to_safe_defaults(tmp_path) -> None:  # type: ignore[no-untyped-def]
    limits = load_risk_limits(tmp_path / "nope.json")
    assert limits.max_risk_per_trade_pct == 1.0
    assert limits.allow_leverage is False


def test_unknown_config_keys_are_ignored(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "c.json"
    path.write_text('{"risk_limits_v1": {"max_position_pct": 3.0, "future_field": 1}}')
    assert load_risk_limits(path).max_position_pct == 3.0


def test_engine_still_refuses_leverage_shorts_and_options() -> None:
    for bad in ("allow_leverage", "allow_short", "allow_options"):
        with pytest.raises(ValueError, match="forbids"):
            RiskEngine(RiskLimits(**{bad: True}))
