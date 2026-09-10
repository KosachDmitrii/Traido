"""IEX observation and entry are possible without consolidated-volume assumptions."""

from decimal import Decimal
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OpportunityStatus, TradingMode, UserDecision
from risk.risk_engine import RiskEngine
from strategy.orb import evaluate_trigger, form_plan
from strategy.orb.runtime import discover
from tests.orb_support import orb_ready_candidate
from tests.support import CLEARED_EARNINGS, admission_ready_candidate, liquid_market_data
from tests.unit.test_orb_policy import NOW, evidence, quote
from tests.unit.test_orb_session import Context, Universe
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.opportunities import MemoryOpportunityStore


def test_iex_volume_is_compared_with_iex_history_without_multiplying_it():
    daily, opening = evidence()
    daily = [b.model_copy(update={"volume": Decimal(250000)}) for b in daily]
    opening = [b.model_copy(update={"volume": b.volume / 20}) for b in opening]
    decision = form_plan("AAPL", daily, opening, now=NOW, feed="iex")
    assert decision.plan is not None
    assert decision.plan.mean_daily_volume == 250000
    assert decision.plan.relative_volume == 2
    assert decision.plan.source == "alpaca:iex"
    assert form_plan("AAPL", daily, opening, now=NOW, feed="sip").reasons == [
        "ORB_DAILY_VOLUME_LOW"
    ]
    q = quote().model_copy(
        update={
            "feed": "iex",
            "bid": decision.plan.max_entry - Decimal("0.02"),
            "ask": decision.plan.max_entry,
        }
    )
    assert evaluate_trigger(decision.plan, q, now=NOW).state == "BUY_ALLOWED"
    assert evaluate_trigger(
        decision.plan, q.model_copy(update={"feed": "sip"}), now=NOW
    ).reasons == ["ORB_DATA_FEED_MISMATCH"]


@pytest.mark.asyncio
async def test_selection_can_run_with_iex_and_cannot_reuse_sip_plan():
    ctx = Context(["AAPL"])
    ctx.market_data._feed = "iex"
    first = await discover(ctx, Universe(["AAPL"]), now=NOW)
    assert first["status"] == "ready"
    assert first["feed"] == "iex"
    assert first["plans"]["AAPL"]["source"] == "alpaca:iex"
    ctx.market_data._feed = "sip"
    changed = await discover(ctx, Universe(["AAPL"]), now=NOW)
    assert changed["reason"] == "ORB_SESSION_CONFIGURATION_CHANGED"
    assert changed["plans"] == first["plans"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
async def test_iex_candidate_passes_real_approval_and_receives_protection():
    broker = MockPaperBroker()
    card = orb_ready_candidate(admission_ready_candidate(), feed="iex")
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    store = MemoryOpportunityStore()
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    market = liquid_market_data(price=float(card.entry))
    market._feed = "iex"
    service = ExecutionService(
        broker=broker,
        market_data=market,
        store=store,
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )
    result = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    assert result.status is OpportunityStatus.EXECUTED
    assert any(o.order_type.value == "stop" for o in broker.orders)
    assert result.candidate.orb_plan["source"] == "alpaca:iex"


@pytest.mark.asyncio
async def test_alpaca_adapter_requests_and_labels_iex_without_sip(monkeypatch):
    import httpx

    from core.enums import Timeframe
    from market_data.providers import alpaca

    calls = []

    async def get(client, url, *, params=None, headers=None):
        calls.append(dict(params or {}))
        if "quotes/latest" in url:
            body = {"quote": {"bp": 101.02, "ap": 101.04, "t": NOW.isoformat()}}
        else:
            body = {
                "bars": {
                    "AAPL": [
                        {"o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000, "t": NOW.isoformat()}
                    ]
                }
            }
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(alpaca, "_paced_get", get)
    port = alpaca.AlpacaMarketData(
        api_key="test", api_secret="test", base_url="https://data.alpaca.markets", feed="iex"
    )
    q = await port.get_quote("AAPL")
    assert q.feed == "iex"
    await port.get_bars_batch(["AAPL"], NOW, NOW, Timeframe.M5)
    assert len(calls) == 2
    assert all(c["feed"] == "iex" for c in calls)


def test_iex_denial_does_not_request_a_sip_subscription():
    import httpx

    from strategy.orb.data_access import data_error_reason

    response = httpx.Response(
        403, text="forbidden", request=httpx.Request("GET", "https://data.alpaca.markets/test")
    )
    error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
    assert data_error_reason(error, feed="iex") == "ORB_DATA_ACCESS_DENIED"


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
@pytest.mark.parametrize("above_limit", [False, True])
async def test_pullback_execution_never_sends_buy_above_reference(above_limit):
    from strategy.orb import VERSION

    broker = MockPaperBroker()
    card = orb_ready_candidate(admission_ready_candidate(), feed="iex", version=VERSION)
    risk = RiskEngine().evaluate(card, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    store = MemoryOpportunityStore()
    opp = store.create(card, risk, TradingMode.CONFIRMATION)
    market = liquid_market_data(price=float(card.entry) + (0.10 if above_limit else -0.02))
    market._feed = "iex"
    service = ExecutionService(
        broker=broker,
        market_data=market,
        store=store,
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )

    async def approve():
        return await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    if above_limit:
        with pytest.raises(RuntimeError, match="ORB_WAITING_PULLBACK"):
            await approve()
        assert broker.orders == []
    else:
        result = await approve()
        assert result.status is OpportunityStatus.EXECUTED
        buys = [o for o in broker.orders if o.side.value == "buy"]
        assert len(buys) == 1
        assert buys[0].order_type.value == "limit"
        assert buys[0].limit_price == Decimal(card.orb_plan["trigger"])
        assert any(o.order_type.value == "stop" for o in broker.orders)
