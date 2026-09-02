"""Stage 5 — position ledger, journal close, review analytics."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import OpportunityStatus, TradeAction, TradingMode, UserDecision
from core.schemas import ExitProposal, TradeCandidate
from database.session import init_db
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ExecutionService
from trading.exits import EXIT_SOLD, MemoryExitStore
from trading.ledger import PositionLedger
from trading.opportunities import MemoryOpportunityStore


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(110),
        risk_reward=2.0,
        reasons=["test setup"],
        strategy_version="strategy_blend@0.1.0",
        pipeline_run_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_approve_opens_ledger_and_exit_journals(tmp_path) -> None:
    set_kill_switch(False)
    eng = create_engine(f"sqlite:///{tmp_path / 's5.db'}", future=True)
    init_db(eng)
    ledger = PositionLedger(engine=eng)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    exits = MemoryExitStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    # Patch LEDGER used inside ExecutionService via monkeypatch on module
    import trading.ledger as ledger_mod

    original = ledger_mod.LEDGER
    ledger_mod.LEDGER = ledger
    try:
        service = ExecutionService(
            market_data=liquid_market_data(),
            broker=broker,
            audit=InMemoryAudit(),
            store=store,
            exit_store=exits,
        )
        result = await service.decide(opp.id, UserDecision.APPROVE)
        assert result.status == OpportunityStatus.EXECUTED
        open_rows = ledger.get_open("AAPL")
        assert len(open_rows) == 1
        assert Decimal(str(open_rows[0].target_price)) >= Decimal(110)

        proposal = ExitProposal(
            position_id=open_rows[0].id,
            symbol="AAPL",
            entry=Decimal(100),
            current=Decimal(112),
            pnl_pct=12.0,
            reasons=["Target reached"],
            recommendation=UserDecision.SELL,
            confidence=0.9,
        )
        item = exits.upsert(proposal)
        broker.marks["AAPL"] = Decimal(112)
        sold = await service.decide_exit(item.id, UserDecision.SELL)
        assert sold.status == EXIT_SOLD
        assert ledger.get_open("AAPL") == []

        from agents.review.agent import build_review

        report = build_review(live_only=True, engine=eng)
        assert report.trade_count == 1
        assert report.win_count == 1
        assert report.expectancy is not None and report.expectancy > 0
    finally:
        ledger_mod.LEDGER = original


def test_review_empty_journal(tmp_path) -> None:
    from agents.review.agent import build_review

    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    init_db(eng)
    report = build_review(live_only=True, engine=eng)
    assert report.trade_count == 0
    assert report.notes


@pytest.mark.asyncio
async def test_review_api(monkeypatch) -> None:
    monkeypatch.setenv("TRAIDO_AUTH_DISABLED", "true")
    monkeypatch.setenv("TRAIDO_BROKER_MOCK", "true")
    from api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.json()["stage"] >= 5
        rev = await client.get("/api/v1/review")
        assert rev.status_code == 200
        body = rev.json()
        assert "trade_count" in body
        assert "notes" in body
        pos = await client.get("/api/v1/positions")
        assert pos.status_code == 200
        assert "positions" in pos.json()
