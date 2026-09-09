"""Observation eligibility is separate from permission to enter now."""

from decimal import Decimal

import pytest

from core.enums import AdmissionDecision, EntryDecision, RiskVerdict, SetupType, TradeAction
from core.schemas import TradeAdmissionResult, TradeCandidate
from tests.unit.test_buy_confirmation_policy import _admit, _bundle, _quote
from trading.entry_policy import set_entry_aggressiveness
from trading.pre_watch_eligibility import admission_for_wait_plan, evaluate_pre_watch_eligibility
from trading.wait_plan import derive_wait_levels, needs_wait_plan


@pytest.mark.parametrize("quality", [0, 25, 49])
@pytest.mark.parametrize("level", [0, 50, 100])
def test_weak_current_entry_waits_but_cannot_buy(quality, level):
    set_entry_aggressiveness(level, actor="test")
    admission = _admit(_bundle(entry_q=quality))
    assert admission.decision is AdmissionDecision.WAIT
    assert not admission.admitted and not admission.buy_ready
    assert "CANDIDATE_ENTRY_BELOW_FLOOR" in admission.reason_codes
    assert evaluate_pre_watch_eligibility(admission, risk_verdict=RiskVerdict.PASS).eligible


@pytest.mark.parametrize("failure", ["target", "stop", "stale", "setup", "momentum", "volume"])
def test_bad_entry_score_does_not_hide_other_failures(failure):
    from datetime import UTC, datetime, timedelta

    bundle = _bundle(
        entry_q=25,
        setup_q=0 if failure == "setup" else 70,
        momentum=-0.5 if failure == "momentum" else 0.15,
        vol_ratio=1.7 if failure == "volume" else 0.9,
    )
    kwargs = {}
    if failure == "target":
        kwargs["target"] = 95.0
    elif failure == "stop":
        kwargs["stop"] = 105.0
    elif failure == "stale":
        kwargs["quote"] = _quote(99.98, 100, ts=datetime.now(UTC) - timedelta(days=1))
    admission = _admit(bundle, **kwargs)
    assert admission.decision in {AdmissionDecision.NO_TRADE, AdmissionDecision.DATA_BLOCKED}
    assert not admission.admitted
    assert not evaluate_pre_watch_eligibility(admission, risk_verdict=RiskVerdict.PASS).eligible


def test_zone_pass_is_stored_as_wait_without_execution_permission():
    original = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        buy_ready=True,
        reason_codes=["BUY_READY_CANDIDATE"],
    )
    wait = admission_for_wait_plan(original)
    assert wait.decision is AdmissionDecision.WAIT
    assert not wait.admitted and not wait.buy_ready
    assert original.admitted  # immutable original assessment
    assert evaluate_pre_watch_eligibility(wait, risk_verdict=RiskVerdict.PASS).eligible
    assert not evaluate_pre_watch_eligibility(
        wait, risk_verdict=RiskVerdict.REJECT, risk_reasons=["MAX_WEEKLY_LOSS"]
    ).eligible


@pytest.mark.parametrize("decision", [AdmissionDecision.NO_TRADE, AdmissionDecision.DATA_BLOCKED])
def test_plan_normalization_never_rescues_failed_admission(decision):
    admission = TradeAdmissionResult(decision=decision, admitted=False)
    assert admission_for_wait_plan(admission) is admission


def test_extended_pullback_uses_zone_plan_even_if_initial_label_was_buy():
    bundle = _bundle(price=104, zone_low=99, zone_high=101, atr=2)
    candidate = TradeCandidate(
        symbol="TEST",
        action=TradeAction.BUY,
        confidence=0.7,
        entry=Decimal(104),
        stop=Decimal(90),
        target=Decimal(120),
        risk_reward=16 / 14,
        reasons=["test"],
        strategy_version="test",
        entry_decision=EntryDecision.BUY_NOW,
        setup_type=SetupType.PULLBACK_CONTINUATION,
    )
    assert needs_wait_plan(bundle, candidate)
    plan = derive_wait_levels(bundle, candidate)
    assert plan.entry == bundle.entry_zone_high
    assert plan.stop < bundle.entry_zone_low
    assert plan.entry < Decimal(str(bundle.facts.current_price))
    assert not needs_wait_plan(bundle.model_copy(update={"entry_zone_high": None}), candidate)
    assert not needs_wait_plan(
        bundle, candidate.model_copy(update={"entry_decision": EntryDecision.NO_TRADE})
    )
    assert not needs_wait_plan(_bundle(price=100, zone_low=99, zone_high=101), candidate)
