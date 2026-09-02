"""LLY / NEM Final Admission regressions — sector and geometry hard gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    AdmissionDecision,
    DataHealthStatus,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
    Timeframe,
    TradingMode,
    UserDecision,
)
from core.schemas import Bar, Quote
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, admission_ready_candidate, liquid_market_data
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.intents import MemoryOrderIntentStore
from trading.opportunities import MemoryOpportunityStore
from trading.sector_assessment import assess_from_benchmark_bars, set_sector_assessment_port
from trading.sector_classification import classify_symbol
from trading.sector_policy import BENCHMARK_MIN_BARS

pytestmark = pytest.mark.usefixtures("capital_path_ready")


def _bars(symbol: str, n: int, *, trend: float, now: datetime) -> list[Bar]:
    out: list[Bar] = []
    price = 100.0
    for i in range(n):
        ts = now - timedelta(days=n - i)
        price *= 1.0 + trend
        px = Decimal(str(round(price, 4)))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=ts,
                open=px,
                high=px * Decimal("1.01"),
                low=px * Decimal("0.99"),
                close=px,
                volume=Decimal("1e6"),
                source="test",
            )
        )
    return out


@pytest.mark.asyncio
async def test_static_classification_never_grants_tradable() -> None:
    nem = classify_symbol("NEM")
    lly = classify_symbol("LLY")
    assert nem.benchmark == "GDX"
    assert lly.benchmark == "XLV"
    assert "tradable_long" not in type(nem).model_fields


@pytest.mark.asyncio
async def test_nem_gdx_blocked_is_no_trade() -> None:
    now = datetime.now(UTC)
    cls = classify_symbol("NEM")
    result = assess_from_benchmark_bars(
        cls, _bars("GDX", BENCHMARK_MIN_BARS + 20, trend=-0.005, now=now), now=now
    )
    assert result.tradable_long is False
    assert result.data_status is DataHealthStatus.HEALTHY
    assert "SECTOR_BLOCKED" in result.reason_codes


@pytest.mark.asyncio
async def test_nem_gdx_missing_is_data_blocked() -> None:
    now = datetime.now(UTC)
    result = assess_from_benchmark_bars(classify_symbol("NEM"), [], now=now)
    assert result.tradable_long is None
    assert result.data_status is DataHealthStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_lly_xlv_stale_is_data_blocked() -> None:
    now = datetime.now(UTC)
    bars = _bars("XLV", BENCHMARK_MIN_BARS + 10, trend=0.004, now=now - timedelta(days=10))
    result = assess_from_benchmark_bars(classify_symbol("LLY"), bars, now=now)
    assert result.tradable_long is None
    assert "SECTOR_BENCHMARK_STALE" in result.reason_codes


@pytest.mark.asyncio
async def test_lly_unrealistic_target_zero_broker_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    candidate = admission_ready_candidate(
        symbol="LLY",
        entry=100.0,
        stop=95.0,
        target=101.0,  # tiny reward → insufficient / unrealistic path
        target_reachability=TargetReachabilityClass.UNREALISTIC,
    )
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    before = len(intents.list_by_key_prefix(f"entry:{opp.id}:"))
    svc = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=intents,
        market_data=liquid_market_data(price=100.0),
    )
    with pytest.raises((RuntimeError, Exception)):
        await svc.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert len(intents.list_by_key_prefix(f"entry:{opp.id}:")) == before
    buy_calls = [
        c
        for c in place.call_args_list
        if getattr(getattr(c.args[0], "side", None), "value", None) == "buy"
        or str(getattr(c.args[0], "side", "")).lower().endswith("buy")
    ]
    assert buy_calls == []


@pytest.mark.asyncio
async def test_sector_blocked_cannot_be_overridden_by_setup_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_kill_switch(False)

    class _BlockedSector:
        async def assess(self, symbol, *, market_data=None, symbol_bars=None, now=None):
            from trading.sector_assessment import SectorMarketAssessment

            evaluated_at = now or datetime.now(UTC)
            return SectorMarketAssessment(
                symbol=symbol.upper(),
                sector="healthcare",
                industry="pharma",
                benchmark="XLV",
                benchmark_bars_count=80,
                benchmark_last_bar_ts=evaluated_at,
                evaluated_at=evaluated_at,
                data_status=DataHealthStatus.HEALTHY,
                sector_regime=None,
                tradable_long=False,
                reason_codes=("SECTOR_BLOCKED",),
            )

    set_sector_assessment_port(_BlockedSector())
    broker = MockPaperBroker()
    place = AsyncMock(wraps=broker.place_order)
    broker.place_order = place
    store = MemoryOpportunityStore()
    intents = MemoryOrderIntentStore()
    candidate = admission_ready_candidate(symbol="LLY")
    # High setup score must not compensate.
    candidate = candidate.model_copy(update={"setup_quality": 99, "confidence": 0.99})
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    before = len(intents.list_by_key_prefix(f"entry:{opp.id}:"))
    svc = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        intents=intents,
        market_data=liquid_market_data(),
    )
    with pytest.raises(Exception):
        await svc.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert len(intents.list_by_key_prefix(f"entry:{opp.id}:")) == before
    assert place.call_count == 0
    set_sector_assessment_port(None)


@pytest.mark.asyncio
async def test_approve_without_request_id_refused() -> None:
    from trading.approval_errors import StaleDecisionError

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    candidate = admission_ready_candidate()
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    svc = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        market_data=liquid_market_data(),
    )
    with pytest.raises(StaleDecisionError, match="APPROVAL_IDENTITY"):
        await svc.decide(opp.id, UserDecision.APPROVE)
