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
