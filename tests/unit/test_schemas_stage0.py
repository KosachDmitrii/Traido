"""Stage 0 — schema geometry & risk config invariants (money-path contracts)."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.enums import TradeAction, TradingMode
from core.schemas import RiskLimits, TradeCandidate
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS


def _candidate(**overrides: object) -> TradeCandidate:
    base = {
        "symbol": "AAPL",
        "action": TradeAction.BUY,
        "confidence": 0.86,
        "entry": Decimal("190.00"),
        "stop": Decimal("185.00"),
        "target": Decimal("205.00"),
        "risk_reward": 3.0,
        "reasons": ["breakout with rising volume"],
        "strategy_version": "stub@0.0.0",
        "pipeline_run_id": uuid4(),
    }
    base.update(overrides)
    return TradeCandidate(**base)  # type: ignore[arg-type]


def test_buy_geometry_valid() -> None:
    c = _candidate()
    assert c.action == TradeAction.BUY
    assert c.stop < c.entry < c.target


def test_buy_geometry_rejects_bad_stop() -> None:
    with pytest.raises(ValidationError):
        _candidate(stop=Decimal("195.00"))


def test_trade_candidate_requires_reasons() -> None:
    with pytest.raises(ValidationError):
        _candidate(reasons=[])


def test_risk_engine_v1_forbids_leverage() -> None:
    with pytest.raises(ValueError, match="forbids"):
        RiskEngine(RiskLimits(allow_leverage=True))


def test_risk_evaluate_pass_and_kill_switch() -> None:
    from core.enums import RiskVerdict
    from core.schemas import PortfolioSnapshot

    engine = RiskEngine()
    portfolio = PortfolioSnapshot(
        equity=Decimal(100000),
        cash=Decimal(100000),
        buying_power=Decimal(100000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
    )
    decision = engine.evaluate(_candidate(), portfolio, context=CLEARED_EARNINGS)
    assert decision.verdict == RiskVerdict.PASS
    assert decision.sized_qty is not None and decision.sized_qty > 0

    blocked = engine.evaluate(
        _candidate(),
        portfolio.model_copy(update={"kill_switch": True}),
    )
    assert blocked.verdict == RiskVerdict.REJECT
    assert "KILL_SWITCH" in blocked.reasons


def test_confirmation_is_default_mode_literal() -> None:
    assert TradingMode.CONFIRMATION.value == "confirmation"
    assert TradingMode.AUTOPILOT.value == "autopilot"
