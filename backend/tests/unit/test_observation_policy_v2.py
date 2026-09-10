"""Observation cannot acquire execution authority; replay real refusal labels separately."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.trader.risk_plan import run_risk_plan
from agents.trader.types import TraderBundle
from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    AdmissionDecision,
    NewsCheck,
    RiskVerdict,
    Timeframe,
    TradingMode,
    UserDecision,
)
from core.schemas import Bar, TradeAdmissionResult
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, admission_ready_candidate, liquid_market_data
from tests.unit.test_capital_safety import _candidate, _portfolio
from tests.unit.test_lly_nem_final_admission import _bars
from tests.unit.test_trader_desk_steps import _snap
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.intents import MemoryOrderIntentStore
from trading.observation_audit import (
    compare_structure,
    record_observation_evidence,
    replay_features,
)
from trading.observation_policy import observation_execution_reasons
from trading.observation_replay import evaluate_forward_path
from trading.opportunities import MemoryOpportunityStore
from trading.pre_watch_eligibility import evaluate_pre_watch_eligibility


@pytest.mark.parametrize(
    "structure,ema,h4,old,new",
    [
        ("uptrend", False, "range", False, True),  # SMCI recorded facts
        ("range", True, "downtrend", False, True),  # MRK recorded facts
        ("downtrend", True, "range", False, False),  # SLV recorded facts
        ("range", False, "range", False, False),
        ("uptrend", True, "uptrend", True, True),
        (None, True, "range", False, False),
    ],
)
def test_structure_same_inputs_separate_observation(structure, ema, h4, old, new):
    bundle = TraderBundle(
        symbol="TEST",
        features={
            Timeframe.D1: _snap(structure=structure, ema_ok=ema),
            Timeframe.H4: _snap(structure=h4),
        },
    )
    compared = compare_structure(bundle)
    assert compared["baseline_pass"] is old
    assert compared["observation_pass"] is new
    if new and not old:
        assert compared["requirements"] == ["DESK_STRUCTURE_CONFIRMATION"]


@pytest.mark.parametrize("rr,observed", [(1.44, False), (1.45, True), (1.93, True), (2.0, True)])
def test_rr_floor_is_observation_only(rr, observed):
    # Same geometry on both policies; the target is never moved to pass a gate.
    plan = (100.0, 95.0, 100.0 + 5 * rr)
    strict = TraderBundle(symbol="TEST", features={Timeframe.D1: _snap()}, _planned=plan)
    watch = TraderBundle(
        symbol="TEST", features=strict.features, _planned=plan, observation_mode=True
    )
    assert run_risk_plan(strict).ok is (rr >= 2)
    assert run_risk_plan(watch).ok is observed
    assert watch._planned == plan
    if observed and rr < 2:
        assert watch.observation_requirements == ["DESK_RR_CONFIRMATION"]


def test_missing_account_history_allows_observation_but_risk_still_rejects():
    candidate = _candidate()
    engine = RiskEngine()
    portfolio = _portfolio(week_pnl=None, drawdown_pct=None)
    risk = engine.evaluate(candidate, portfolio, context=CLEARED_EARNINGS)
    assert risk.verdict is RiskVerdict.REJECT and risk.sized_qty is None
    stable = engine.observation_reasons(candidate, CLEARED_EARNINGS)
    assert stable == []
    admission = TradeAdmissionResult(decision=AdmissionDecision.WAIT, admitted=False)
    assert evaluate_pre_watch_eligibility(admission, observation_risk_reasons=stable).eligible
    assert portfolio.week_pnl is None and portfolio.drawdown_pct is None


def test_unread_news_does_not_pass_observation():
    from dataclasses import replace

    ctx = replace(CLEARED_EARNINGS, news=NewsCheck.UNAVAILABLE)
    reasons = RiskEngine().observation_reasons(_candidate(), ctx)
    assert "NEWS_UNAVAILABLE" in reasons
    assert not evaluate_pre_watch_eligibility(
        TradeAdmissionResult(decision=AdmissionDecision.WAIT, admitted=False),
        observation_risk_reasons=reasons,
    ).eligible


@pytest.mark.asyncio
async def test_missing_confirmation_bars_fail_closed():
    candidate = admission_ready_candidate().model_copy(
        update={"observation_requirements": ["DESK_STRUCTURE_CONFIRMATION"]}
    )
    md = AsyncMock()
    md.get_bars.return_value = []
    assert await observation_execution_reasons(candidate, md) == [
        "OBSERVATION_CONFIRMATION_DATA_MISSING"
    ]


@pytest.mark.asyncio
async def test_rr_confirmation_can_clear_only_after_actual_geometry_improves():
    md = AsyncMock()
    candidate = admission_ready_candidate(entry=100, stop=95, target=108).model_copy(
        update={"observation_requirements": ["DESK_RR_CONFIRMATION"]}
    )
    assert await observation_execution_reasons(candidate, md) == ["DESK_RR_CONFIRMATION"]
    improved = candidate.model_copy(update={"entry": Decimal(99), "risk_reward": 2.25})
    assert await observation_execution_reasons(improved, md) == []
    md.get_bars.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
async def test_observation_only_candidate_cannot_create_order():
    set_kill_switch(False)
    broker = MockPaperBroker()
    broker.place_order = AsyncMock(wraps=broker.place_order)
    candidate = admission_ready_candidate().model_copy(
        update={"observation_requirements": ["UNRECOGNIZED_REQUIREMENT"]}
    )
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    svc = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=intents,
        market_data=liquid_market_data(price=100),
    )
    with pytest.raises(RuntimeError, match="STRATEGY_RETIRED:ORB_REQUIRED"):
        await svc.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert intents.list_by_key_prefix(f"entry:{opp.id}:") == []
    broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_captured_bars_recompute_and_tampered_features_are_detected():
    from core.schemas import PipelineResult
    from quant.engine import compute_features

    bars = _bars("TEST", 230, trend=0.001, now=datetime.now(UTC))
    bundle = TraderBundle(
        symbol="TEST",
        source_bars={Timeframe.D1.value: bars},
        features={Timeframe.D1: compute_features("TEST", Timeframe.D1, bars)},
    )
    audit = InMemoryAudit()
    await record_observation_evidence(
        bundle, PipelineResult(pipeline_run_id=uuid4(), symbol="TEST", status="no_candidate"), audit
    )
    evidence = audit.events[0]["payload"]
    assert replay_features(evidence)["verified"]
    evidence["features"][Timeframe.D1.value]["indicators"]["ema_50"] = 1
    assert replay_features(evidence)["differences"] == [
        f"{Timeframe.D1.value}:indicators",
        f"{Timeframe.D1.value}:independent_ema_50",
    ]


def test_forward_evaluation_never_uses_predecision_bars_or_assumes_intrabar_order():
    now = datetime.now(UTC)

    def bar(ts, low, high):
        return Bar(
            symbol="TEST",
            timeframe=Timeframe.H1,
            ts=ts,
            open=Decimal(100),
            close=Decimal(100),
            low=Decimal(low),
            high=Decimal(high),
            volume=Decimal(1000),
            source="test",
        )

    geometry = {
        "decided_at": now,
        "entry": Decimal(100),
        "stop": Decimal(95),
        "target": Decimal(110),
    }
    assert (
        evaluate_forward_path(**geometry, bars=[bar(now - timedelta(hours=1), 90, 120)])["status"]
        == "no_forward_data"
    )
    bars = [bar(now + timedelta(hours=1), 99, 101), bar(now + timedelta(hours=2), 90, 120)]
    assert evaluate_forward_path(**geometry, bars=bars)["status"] == "ambiguous_bar"
    bars[-1] = bar(now + timedelta(hours=2), 94, 103)
    assert evaluate_forward_path(**geometry, bars=bars)["status"] == "stop_first"


@pytest.mark.asyncio
@pytest.mark.parametrize("downtrend", [False, True])
async def test_deferred_structure_is_recomputed_from_fresh_alpaca_bars(downtrend):
    import math

    now = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
    d1 = _bars("TEST", 230, trend=0.001, now=now)
    h1 = []
    for i in range(400):
        px = Decimal(str(250 + (-0.2 if downtrend else 0.2) * i + 3 * math.sin(i * math.pi / 8)))
        h1.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.H1,
                ts=now - timedelta(hours=400 - i),
                open=px,
                high=px + 1,
                low=px - 1,
                close=px,
                volume=Decimal(10000),
                source="test",
            )
        )

    class MD:
        async def get_bars(self, symbol, tf, start, end):
            return d1 if tf is Timeframe.D1 else h1

    candidate = admission_ready_candidate(symbol="TEST").model_copy(
        update={"observation_requirements": ["DESK_STRUCTURE_CONFIRMATION"]}
    )
    reasons = await observation_execution_reasons(candidate, MD(), now=now)
    assert reasons == (["DESK_STRUCTURE_CONFIRMATION"] if downtrend else [])
