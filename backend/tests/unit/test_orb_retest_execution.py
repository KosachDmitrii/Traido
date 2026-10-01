"""Real final admission/execution, using synthetic vendor inputs and a mock broker."""

from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    EntryDecision,
    OpportunityStatus,
    SetupType,
    Timeframe,
    TradingMode,
    UserDecision,
)
from core.schemas import TradeCandidate
from database.models.orb import OrbSessionRow
from database.session import session_factory
from risk.risk_engine import RiskEngine
from strategy.orb.retest import rebuild
from tests.conftest import RTH_INSTANT
from tests.support import CLEARED_EARNINGS, LiquidMarketData
from tests.unit.test_orb_retest import scenario
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.opportunities import MemoryOpportunityStore


def current_plan(version=None):
    base, pattern, _ = scenario(RTH_INSTANT)
    from core.schemas import Bar
    from strategy.orb import INTRADAY_VERSION, form_plan

    if version == INTRADAY_VERSION:
        base = form_plan(
            base.symbol,
            [Bar.model_validate(b) for b in base.evidence["daily"]],
            [
                Bar.model_validate(b).model_copy(
                    update={"ts": Bar.model_validate(b).ts + timedelta(minutes=70)}
                )
                for b in base.evidence["opening"]
            ],
            now=RTH_INSTANT,
            feed="sip",
            version=version,
        ).plan
        assert base is not None
    prefix = [
        pattern[0].model_copy(
            update={
                "ts": base.range_end + timedelta(minutes=i * 5),
                "open": D("100.6"),
                "high": D("100.9"),
                "low": D("100.5"),
                "close": D("100.7"),
            }
        )
        for i in range(14)
    ]
    rows = ([] if version == INTRADAY_VERSION else prefix) + [
        b.model_copy(update={"ts": b.ts + timedelta(minutes=70)}) for b in pattern
    ]
    plan = rebuild(base, rows, now=RTH_INSTANT).plan
    assert plan.evidence.get("retest")
    with session_factory()() as db:
        db.add(
            OrbSessionRow(
                session=plan.session,
                payload={
                    "session": plan.session,
                    "version": plan.version,
                    "plans": {plan.symbol: plan.model_dump(mode="json")},
                    "states": {},
                },
            )
        )
        db.commit()
    return plan, rows


class RetestMarket(LiquidMarketData):
    def __init__(self, plan, rows):
        super().__init__(price=101.10)
        self.plan, self.rows = plan, rows

    async def get_bars(self, symbol, timeframe, start, end):
        if (
            self.plan.version == "orb@2.3.0"
            and timeframe == Timeframe.M5
            and start == self.plan.range_start
        ):
            from core.schemas import Bar

            return [Bar.model_validate(self.plan.evidence["opening"][-1])]
        if timeframe == Timeframe.M5 and start >= self.plan.range_end:
            return [b for b in self.rows if start <= b.ts <= end]
        return await super().get_bars(symbol, timeframe, start, end)


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
@pytest.mark.parametrize(
    "failure", [None, "expensive", "missing_bar", "changed_confirmation", "stale_quote", "geared"]
)
@pytest.mark.parametrize("manual_target", [False, True])
@pytest.mark.parametrize("version", [None, "orb@2.3.0"])
async def test_retest_passes_real_execution_or_has_no_broker_effect(
    failure, manual_target, version, monkeypatch
):
    from core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "paper_exit_policy", "manual_target" if manual_target else "protected"
    )
    plan, rows = current_plan(version)
    if failure == "geared":
        from copy import deepcopy

        evidence = deepcopy(plan.evidence)
        evidence["instrument"] = {
            "classification_evidence": {"asset_name": "Direxion Daily Small Cap Bear 3X ETF"}
        }
        plan = plan.model_copy(update={"evidence": evidence})
        with session_factory()() as db:
            row = db.get(OrbSessionRow, plan.session)
            payload = deepcopy(row.payload)
            payload["plans"][plan.symbol] = plan.model_dump(mode="json")
            row.payload = payload
            db.commit()
    candidate = TradeCandidate(
        symbol=plan.symbol,
        action="buy",
        confidence=0,
        entry=plan.max_entry,
        stop=plan.stop,
        exit_policy="session_close",
        exit_at=plan.exit_at,
        orb_plan=plan.model_dump(mode="json"),
        reasons=["ORB_RETEST_CONFIRMED"],
        strategy_version=plan.version,
        exec_timeframe=Timeframe.M5,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        entry_decision=EntryDecision.BUY_NOW,
        admission_version=plan.version,
        policy_version=plan.version,
    )
    broker = MockPaperBroker()
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    store = MemoryOpportunityStore()
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    market = RetestMarket(plan, rows)
    if failure == "expensive":
        market.price = 101.5
    if failure == "missing_bar":
        market.rows = rows[1:]
    if failure == "changed_confirmation":
        market.rows = rows[:-1] + [rows[-1].model_copy(update={"close": D("101.11")})]
    if failure == "stale_quote":
        market._now = lambda: RTH_INSTANT - timedelta(seconds=10)
    service = ExecutionService(
        broker=broker,
        market_data=market,
        store=store,
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )
    before = service.intents.list_by_key_prefix("entry:")
    if failure:
        with pytest.raises((RuntimeError, ValueError)):
            await service.decide(
                opp.id,
                UserDecision.APPROVE,
                request_id=uuid4(),
                expected_decision_version=opp.decision_version,
            )
        assert service.intents.list_by_key_prefix("entry:") == before
        assert broker.orders == []
    else:
        result = await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
        assert result.status == OpportunityStatus.EXECUTED
        buys = [o for o in broker.orders if o.side.value == "buy"]
        assert len(buys) == 1 and buys[0].limit_price <= plan.max_entry
        assert any(
            o.order_type.value == "stop" and o.stop_price == plan.stop for o in broker.orders
        ) is (not manual_target)
        if manual_target:
            assert len(broker.orders) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
async def test_runtime_publishes_once_and_skip_needs_a_new_pattern(monkeypatch):
    from datetime import datetime

    from core.config import get_settings
    from core.enums import OpportunityStatus
    from core.schemas import Bar
    from database.session import get_sync_engine
    from strategy.orb import form_plan, publication, runtime
    from strategy.orb.retest_data import _cache
    from strategy.orb.store import read_session
    from trading.opportunities import OPPORTUNITIES
    from trading.scan_context import ScanContext

    plan, rows = current_plan()
    base = form_plan(
        plan.symbol,
        [Bar.model_validate(b) for b in plan.evidence["daily"]],
        [Bar.model_validate(b) for b in plan.evidence["opening"]],
        now=RTH_INSTANT,
        feed="sip",
    ).plan
    with session_factory()() as db:
        saved = db.get(OrbSessionRow, plan.session)
        saved.payload = {**saved.payload, "plans": {plan.symbol: base.model_dump(mode="json")}}
        db.commit()
    clock = [RTH_INSTANT]

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(runtime, "datetime", Frozen)
    monkeypatch.setattr(publication, "datetime", Frozen)
    monkeypatch.setattr(OPPORTUNITIES, "_engine", get_sync_engine())
    monkeypatch.setattr(
        "trading.auto_trigger_policy.enqueue_auto_approve_opportunity", lambda *a, **k: None
    )
    _cache.clear()
    market = RetestMarket(plan, rows)
    market._now = lambda: clock[0]
    ctx = ScanContext(settings=get_settings(), broker=MockPaperBroker(), market_data=market)
    result = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert result.opportunity is not None, (result.status, result.errors)
    oid = result.opportunity.id
    assert result.opportunity.expires_at == RTH_INSTANT + timedelta(minutes=10)
    again = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert again.opportunity.id == oid
    assert OPPORTUNITIES.claim(
        oid,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.SKIPPED,
    )
    clock[0] += timedelta(minutes=1)
    skipped = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert skipped.opportunity is None
    saved = read_session(plan.session)
    assert "opportunity_id" not in saved["states"][plan.symbol]
    assert not saved["plans"][plan.symbol]["evidence"].get("retest")
    assert OPPORTUNITIES.get(oid).status == OpportunityStatus.SKIPPED
    _cache.clear()
