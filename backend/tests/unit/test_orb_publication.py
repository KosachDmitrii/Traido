"""No half-published ORB opportunities after a crash; one attempt per saved plan."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from core.enums import AdmissionDecision, RiskVerdict, TradingMode
from core.schemas import PipelineResult, TradeAdmissionResult
from database.models.desk import OpportunityRow
from database.session import session_factory
from strategy.orb.publication import publish_orb
from strategy.orb.store import read_session
from tests.conftest import RTH_INSTANT
from tests.orb_support import orb_ready_candidate
from tests.support import admission_ready_candidate, fresh_quote
from trading.geometry_hash import geometry_hash_from_candidate


def proposed():
    c = orb_ready_candidate(admission_ready_candidate())
    from risk.risk_engine import RiskEngine
    from tests.support import CLEARED_EARNINGS
    from tests.unit.test_scan_context import _snapshot

    risk = RiskEngine().evaluate(c, _snapshot(), context=CLEARED_EARNINGS)
    assert risk.verdict is RiskVerdict.PASS
    result = PipelineResult(
        pipeline_run_id=uuid4(), symbol=c.symbol, status="risk_passed", candidate=c, risk=risk
    )
    from core.schemas import AdmissionInput

    quote = fresh_quote(c.symbol, float(c.entry), float(c.entry) + 0.01).model_copy(
        update={"ts": RTH_INSTANT}
    )
    admission = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        buy_ready=True,
        admission_version="orb@1.1.0",
    )
    inp = AdmissionInput(
        orb_plan=c.orb_plan,
        quote=quote,
        limit_price=c.entry,
        stop_price=c.stop,
        strategy_version=c.strategy_version,
        evaluated_at=RTH_INSTANT,
        setup_type=c.setup_type,
        setup_quality=0,
        bars_count=15,
        bar_timeframe="5m",
        admission_version=c.strategy_version,
        policy_version=c.strategy_version,
        aggressiveness=0,
        geometry_hash=geometry_hash_from_candidate(c),
    )
    final = SimpleNamespace(
        admission=admission,
        admission_input=inp,
        quote=quote,
        geometry_hash=geometry_hash_from_candidate(c),
    )
    return result, final


def test_publication_and_consumed_plan_are_one_transaction():
    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    again = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    assert again.id == opp.id
    assert opp.creation_admission_record_id is not None and opp.legacy is False
    saved = read_session(result.candidate.orb_plan["session"])
    assert saved["states"][result.symbol]["opportunity_id"] == str(opp.id)
    with session_factory()() as db:
        assert len(list(db.scalars(select(OpportunityRow)))) == 1


def test_admission_write_failure_rolls_back_the_opportunity(monkeypatch):
    from strategy.orb import publication

    result, final = proposed()

    def fail(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(publication.ADMISSION_RECORDS, "record_in_session", fail)
    with pytest.raises(RuntimeError, match="write failed"):
        publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    with session_factory()() as db:
        assert list(db.scalars(select(OpportunityRow))) == []
    assert (
        not read_session(result.candidate.orb_plan["session"])
        .get("states", {})
        .get(result.symbol, {})
        .get("opportunity_id")
    )


def test_skip_requires_cooldown_fresh_reset_and_new_admission():
    from datetime import timedelta
    from decimal import Decimal

    from core.enums import OpportunityStatus
    from strategy.orb.store import rearm_skipped_plan
    from trading.opportunities import OpportunityStore

    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    store = OpportunityStore()
    store.claim(
        opp.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.SKIPPED,
    )
    day, symbol = result.candidate.orb_plan["session"], result.symbol
    below = final.quote.model_copy(
        update={
            "bid": Decimal(result.candidate.orb_plan["trigger"]) - Decimal("0.02"),
            "ask": Decimal(result.candidate.orb_plan["trigger"]),
        }
    )
    assert not rearm_skipped_plan(day, symbol, str(opp.id), below, now=RTH_INSTANT)
    later = RTH_INSTANT + timedelta(seconds=61)
    # A fresh move above the new ceiling is required before another pullback.
    assert not rearm_skipped_plan(
        day, symbol, str(opp.id), below.model_copy(update={"ts": later}), now=later
    )
    assert not rearm_skipped_plan(day, symbol, str(opp.id), below, now=later)
    assert rearm_skipped_plan(
        day, symbol, str(opp.id), final.quote.model_copy(update={"ts": later}), now=later
    )
    assert not rearm_skipped_plan(
        day, symbol, str(opp.id), below.model_copy(update={"ts": later}), now=later
    )
    state = read_session(day)["states"][symbol]
    assert state["skipped_opportunity_ids"] == [str(opp.id)]
    assert "opportunity_id" not in state
    assert store.get(opp.id).status is OpportunityStatus.SKIPPED
    with pytest.raises(ValueError, match="ORB_STALE_REARM_ADMISSION"):
        publish_orb(result, final, TradingMode.CONFIRMATION, now=later)
    final.quote = final.quote.model_copy(update={"ts": later + timedelta(seconds=1)})
    new = publish_orb(result, final, TradingMode.CONFIRMATION, now=later + timedelta(seconds=1))
    assert new.id != opp.id
    assert new.creation_admission_record_id != opp.creation_admission_record_id
    assert (
        publish_orb(result, final, TradingMode.CONFIRMATION, now=later + timedelta(seconds=1)).id
        == new.id
    )


@pytest.mark.parametrize("status", ["executed", "approving", "awaiting_confirmation", "discarded"])
def test_rearm_never_releases_non_skipped_claim(status):
    from core.enums import OpportunityStatus
    from strategy.orb.store import rearm_skipped_plan
    from trading.opportunities import OpportunityStore

    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    if status != "awaiting_confirmation":
        OpportunityStore().claim(
            opp.id,
            from_status=OpportunityStatus.AWAITING_CONFIRMATION,
            to_status=OpportunityStatus(status),
        )
    day = result.candidate.orb_plan["session"]
    assert not rearm_skipped_plan(day, result.symbol, str(opp.id), final.quote, now=RTH_INSTANT)
    assert read_session(day)["states"][result.symbol]["opportunity_id"] == str(opp.id)


@pytest.mark.parametrize("status", ["awaiting_confirmation", "approving", "executed", "skipped"])
def test_pullback_rollout_retires_only_unclaimed_proposal(status):
    from datetime import timedelta
    from decimal import Decimal

    from core.enums import OpportunityStatus
    from strategy.orb import VERSION
    from strategy.orb.store import upgrade_unpublished_entry_limits
    from trading.opportunities import OpportunityStore

    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    store = OpportunityStore()
    if status != "awaiting_confirmation":
        store.claim(
            opp.id,
            from_status=OpportunityStatus.AWAITING_CONFIRMATION,
            to_status=OpportunityStatus(status),
        )
    day = result.candidate.orb_plan["session"]
    before = read_session(day)
    saved = upgrade_unpublished_entry_limits(day, now=RTH_INSTANT + timedelta(seconds=1))
    if status != "awaiting_confirmation":
        assert saved["plans"] == before["plans"]
        assert saved["states"] == before["states"]
        assert store.get(opp.id).status.value == status
        return
    new_plan = saved["plans"][result.symbol]
    assert new_plan["version"] == VERSION
    assert new_plan["max_entry"] == new_plan["trigger"]
    assert Decimal(new_plan["max_entry"]) < Decimal(opp.candidate.orb_plan["max_entry"])
    retired = store.get(opp.id)
    assert retired.status is OpportunityStatus.DISCARDED
    assert retired.candidate == opp.candidate
    assert retired.creation_admission_record_id == opp.creation_admission_record_id
    state = saved["states"][result.symbol]
    assert state["replaced_opportunity_ids"] == [str(opp.id)]
    assert "opportunity_id" not in state
    assert (
        store.claim(
            opp.id,
            from_status=OpportunityStatus.AWAITING_CONFIRMATION,
            to_status=OpportunityStatus.APPROVING,
        )
        is None
    )
    with pytest.raises(ValueError, match="ORB_PLAN_NOT_SELECTED"):
        publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT + timedelta(seconds=2))
    assert upgrade_unpublished_entry_limits(day, now=RTH_INSTANT + timedelta(seconds=2)) == saved


@pytest.mark.parametrize(
    "status,replace",
    [("awaiting_confirmation", True), ("skipped", True), ("approving", False), ("executed", False)],
)
def test_retest_geometry_replacement_cannot_cross_an_execution_claim(status, replace):
    from core.enums import OpportunityStatus
    from strategy.orb.store import replace_unclaimed_plan
    from trading.opportunities import OpportunityStore

    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    store = OpportunityStore()
    if status != "awaiting_confirmation":
        assert store.claim(
            opp.id,
            from_status=OpportunityStatus.AWAITING_CONFIRMATION,
            to_status=OpportunityStatus(status),
        )
    old = result.candidate.orb_plan
    new = dict(old)
    new["name"] = "replacement for CAS test"
    assert (
        replace_unclaimed_plan(
            old["session"], result.symbol, old, new, {"state": "WAIT"}, now=RTH_INSTANT
        )
        is replace
    )
    saved = read_session(old["session"])
    if replace:
        assert saved["plans"][result.symbol] == new
        assert "opportunity_id" not in saved["states"][result.symbol]
        # A stale competing worker cannot overwrite the winning geometry.
        assert not replace_unclaimed_plan(
            old["session"], result.symbol, old, old, {"state": "WAIT"}, now=RTH_INSTANT
        )
    else:
        assert saved["plans"][result.symbol] == old
        assert saved["states"][result.symbol]["opportunity_id"] == str(opp.id)
