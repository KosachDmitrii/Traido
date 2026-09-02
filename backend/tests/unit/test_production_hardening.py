"""Production hardening — P0 characterization tests (Stage 0/1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.enums import (
    AdmissionDecision,
    DataHealthStatus,
    InstrumentThesis,
    SetupType,
    TradeAction,
)
from core.schemas import (
    AdmissionSnapshot,
    PortfolioSnapshot,
    Quote,
    TradeAdmissionResult,
    TradeCandidate,
)
from risk.risk_engine import RiskContext, RiskEngine
from tests.support import CLEARED_EARNINGS
from trading.final_pretrade import (
    PretradeRejection,
    final_pretrade_validation,
    require_final_admission,
)
from trading.market_gate import evaluate_market_gate, evaluate_market_gate_for_candidate


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


def _nem_wait_candidate(*, snap_price: float = 112.0, ask: float = 112.0) -> TradeCandidate:
    snap = AdmissionSnapshot(
        price_at_creation=snap_price,
        atr_at_creation=2.0,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        entry_zone_low=111.8,
        entry_zone_high=113.2,
        setup_quality_at_creation=82,
        entry_quality_at_creation=76,
        stop_at_creation=108.0,
        target_at_creation=125.0,
    )
    return TradeCandidate(
        symbol="NEM",
        action=TradeAction.BUY,
        confidence=0.85,
        entry=Decimal(str(ask)),
        stop=Decimal(108),
        target=Decimal(125),
        risk_reward=2.5,
        reasons=["wait revalidation"],
        strategy_version="test@1",
        thesis=InstrumentThesis.BULLISH,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=82,
        entry_quality=76,
        signal_price=Decimal(123),
        entry_zone_low=Decimal("111.8"),
        entry_zone_high=Decimal("113.2"),
        market_label="neutral",
        admission_version="admission@1.1.0",
        admission_snapshot=snap.model_dump(mode="json"),
    )


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        symbol="NEM",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC),
        source="test",
    )


def test_nem_in_zone_not_blocked_by_signal_distance() -> None:
    """9% from signal is OK when price is in planned zone at revalidation anchor."""
    from core.enums import Timeframe
    from core.schemas import FeatureSnapshot

    cand = _nem_wait_candidate(snap_price=112.0, ask=112.0)
    now = datetime.now(UTC)
    snap = FeatureSnapshot(
        symbol="NEM",
        timeframe=Timeframe.H1,
        computed_at=now,
        indicators={"close": 112.0, "atr_14": 2.0, "sma_20": 111.0, "vwap": 111.5},
        candlestick_patterns={},
        chart_patterns={},
        support=[Decimal(108)],
        resistance=[Decimal(125)],
    )
    try:
        final_pretrade_validation(
            cand,
            quote=_quote(111.95, 112.0),
            bars_count=60,
            last_bar_ts=now,
            now=now,
            exec_snap=snap,
        )
    except PretradeRejection as exc:
        assert exc.code != "BUY_REJECTED_PRICE_MOVED"
        assert "approval_drift" not in exc.detail


def test_market_gate_unknown_label_is_data_blocked() -> None:
    cand = _nem_wait_candidate().model_copy(update={"market_label": "nonsense"})
    gate = evaluate_market_gate_for_candidate(cand)
    assert gate.tradable_long is False
    assert gate.status is DataHealthStatus.UNHEALTHY
    assert any("REGIME_UNKNOWN" in r for r in gate.reason_codes)


def test_missing_regime_blocks_risk_engine() -> None:
    ctx = RiskContext(
        earnings=CLEARED_EARNINGS.earnings,
        news=CLEARED_EARNINGS.news,
        sector=CLEARED_EARNINGS.sector,
        sector_check=CLEARED_EARNINGS.sector_check,
        regime_tradable=None,
    )
    cand = _nem_wait_candidate()
    decision = RiskEngine().evaluate(cand, _portfolio(), context=ctx)
    assert decision.verdict.value != "pass"
    assert "REGIME_MISSING" in decision.reasons


def test_market_gate_missing_is_fail_closed() -> None:
    gate = evaluate_market_gate(None)
    assert gate.tradable_long is False
    assert "REGIME_MISSING" in gate.reason_codes


def test_candidate_without_market_label_blocked() -> None:
    cand = _nem_wait_candidate().model_copy(update={"market_label": None})
    gate = evaluate_market_gate_for_candidate(cand)
    assert gate.tradable_long is False


def test_admission_wait_result_blocks_via_require() -> None:
    blocked = TradeAdmissionResult(
        decision=AdmissionDecision.WAIT,
        admitted=False,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=80,
        entry_quality=70,
        data_status=DataHealthStatus.HEALTHY,
    )
    with pytest.raises(PretradeRejection):
        require_final_admission(blocked)
