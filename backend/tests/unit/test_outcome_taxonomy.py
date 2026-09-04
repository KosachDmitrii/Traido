"""Honest outcome classes — data errors are not NO_TRADE, degraded setup is not WAIT."""

from __future__ import annotations

from core.enums import AdmissionDecision, RiskVerdict
from core.schemas import TradeAdmissionResult
from trading.buy_confirmation import (
    HEAVY_SELL_VOLUME,
    MATERIAL_NEGATIVE_MOMENTUM,
    MOMENTUM_CONFIRMATION_MISSING,
    buy_confirmation_for,
    evaluate_buy_confirmation,
)
from trading.outcome_taxonomy import OutcomeClass, classify_codes, classify_exception_text
from trading.pre_watch_eligibility import evaluate_pre_watch_eligibility


def test_news_unread_is_data_blocked() -> None:
    admission = TradeAdmissionResult(
        decision=AdmissionDecision.WAIT,
        admitted=False,
        reason_codes=["WAITING_CONFIRMATION"],
    )
    elig = evaluate_pre_watch_eligibility(
        admission,
        risk_verdict=RiskVerdict.REJECT,
        risk_reasons=["NEWS_NOT_CONFIGURED"],
    )
    assert elig.eligible is False
    assert elig.outcome == "DATA_BLOCKED"


def test_regime_missing_is_data_blocked() -> None:
    admission = TradeAdmissionResult(
        decision=AdmissionDecision.WAIT,
        admitted=False,
        reason_codes=["WAITING_CONFIRMATION"],
    )
    elig = evaluate_pre_watch_eligibility(
        admission,
        risk_verdict=RiskVerdict.REJECT,
        risk_reasons=["REGIME_MISSING"],
    )
    assert elig.outcome == "DATA_BLOCKED"


def test_earnings_imminent_is_no_trade() -> None:
    admission = TradeAdmissionResult(
        decision=AdmissionDecision.WAIT,
        admitted=False,
        reason_codes=["WAITING_CONFIRMATION"],
    )
    elig = evaluate_pre_watch_eligibility(
        admission,
        risk_verdict=RiskVerdict.REJECT,
        risk_reasons=["EARNINGS_IMMINENT"],
    )
    assert elig.outcome == "NO_TRADE"


def test_reconciliation_stale_is_operational() -> None:
    assert classify_codes(["RECONCILIATION_STALE"]) is OutcomeClass.OPERATIONAL_BLOCKED


def test_material_negative_momentum_is_no_trade_at_every_slider() -> None:
    from tests.unit.test_buy_confirmation_policy import _admit, _bundle
    from trading.entry_policy import set_entry_aggressiveness

    for level in (0, 50, 100):
        set_entry_aggressiveness(level, actor="test")
        admission = _admit(_bundle(momentum=-0.50, setup_q=70, entry_q=65, target=125.0))
        assert admission.decision is AdmissionDecision.NO_TRADE, level
        assert MATERIAL_NEGATIVE_MOMENTUM in admission.reason_codes
        assert MOMENTUM_CONFIRMATION_MISSING not in admission.reason_codes


def test_heavy_sell_volume_is_no_trade_at_every_slider() -> None:
    from tests.unit.test_buy_confirmation_policy import _admit, _bundle
    from trading.entry_policy import set_entry_aggressiveness

    for level in (0, 50, 100):
        set_entry_aggressiveness(level, actor="test")
        admission = _admit(
            _bundle(momentum=0.10, vol_ratio=1.70, setup_q=70, entry_q=65, target=125.0)
        )
        assert admission.decision is AdmissionDecision.NO_TRADE, level
        assert HEAVY_SELL_VOLUME in admission.reason_codes


def test_missing_momentum_stays_wait_on_weak() -> None:
    policy = buy_confirmation_for(100)
    result = evaluate_buy_confirmation(
        policy=policy,
        setup_quality=70,
        entry_quality=65,
        planned_rr=2.0,
        effective_rr=2.0,
        momentum_pct=None,
        pullback_vol_ratio=0.9,
        price=100.0,
        distance_from_vwap_pct=-0.1,
        anchor_price=100.0,
        structure_valid=True,
        paper=True,
        arrival_required=False,
    )
    assert result.passed is True


def test_entry_state_unknown_is_never_retried() -> None:
    assert classify_exception_text("ENTRY_STATE_UNKNOWN:timeout") is OutcomeClass.UNKNOWN


def test_buy_rejected_regime_missing_is_data() -> None:
    assert (
        classify_exception_text("BUY_REJECTED_REGIME:REGIME_MISSING") is OutcomeClass.DATA_BLOCKED
    )


def test_transient_pretrade_book_keeps_wait() -> None:
    assert classify_exception_text("BUY_REJECTED_SPREAD:spread_bps=18.4") is OutcomeClass.WAIT
    assert classify_exception_text("BUY_REJECTED_SPREAD:EXTREME_SPREAD") is OutcomeClass.WAIT
    assert classify_exception_text("BUY_REJECTED_CHASE:chase=72") is OutcomeClass.WAIT
    assert (
        classify_exception_text("BUY_REJECTED_RR_DROPPED:INSUFFICIENT_EFFECTIVE_RR")
        is OutcomeClass.WAIT
    )


def test_buy_rejected_regime_blocked_stays_terminal() -> None:
    assert (
        classify_exception_text("BUY_REJECTED_REGIME:REGIME_BLOCKED")
        is OutcomeClass.TERMINAL_REJECT
    )
