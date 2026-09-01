"""Stage 4 hardening — persist, auth, exit via ExecutionService, idempotency."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

from broker.paper.mock import MockPaperBroker
from core.audit import DbAudit, InMemoryAudit
from core.enums import OpportunityStatus, TradeAction, TradingMode, UserDecision
from core.schemas import ExitProposal, TradeCandidate
from database.models.desk import AuditEventRow
from database.session import init_db
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS
from trading.execution import ExecutionService
from trading.exits import EXIT_SOLD, MemoryExitStore
from trading.opportunities import MemoryOpportunityStore, OpportunityStore


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(110),
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
        pipeline_run_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_sql_opportunity_persists(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'desk.db'}", future=True)
    init_db(eng)
    store = OpportunityStore(engine=eng)
    broker = MockPaperBroker()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    loaded = store.get(opp.id)
    assert loaded is not None
    assert loaded.id == opp.id
    assert loaded.status == OpportunityStatus.AWAITING_CONFIRMATION
    claimed = store.claim(
        opp.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.APPROVING,
    )
    assert claimed is not None
    assert (
        store.claim(
            opp.id,
            from_status=OpportunityStatus.AWAITING_CONFIRMATION,
            to_status=OpportunityStatus.APPROVING,
        )
        is None
    )


@pytest.mark.asyncio
async def test_db_audit_writes_row(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'audit.db'}", future=True)
    init_db(eng)
    audit = DbAudit(engine=eng, mirror_jsonl=False)
    await audit.append("TestEvent", "test", {"ok": True})
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=eng, future=True)
    with SessionLocal() as session:
        rows = session.query(AuditEventRow).all()
        assert len(rows) == 1
        assert rows[0].event_type == "TestEvent"


@pytest.mark.asyncio
async def test_exit_sell_goes_through_execution_service() -> None:
    set_kill_switch(False)
    broker = MockPaperBroker()
    from core.enums import OrderSide, OrderType
    from core.schemas import OrderRequest

    await broker.place_order(
        OrderRequest(
            client_order_id="seed-exit-test",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal(2),
            limit_price=Decimal(100),
            reason="seed position for exit test",
        )
    )
    exits = MemoryExitStore()
    proposal = ExitProposal(
        position_id=uuid4(),
        symbol="AAPL",
        entry=Decimal(100),
        current=Decimal(110),
        pnl_pct=10.0,
        reasons=["Target reached"],
        recommendation=UserDecision.SELL,
        confidence=0.8,
    )
    item = exits.upsert(proposal)
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=MemoryOpportunityStore(),
        exit_store=exits,
    )
    result = await service.decide_exit(item.id, UserDecision.SELL)
    assert result.status == EXIT_SOLD
    assert any(o.side.value == "sell" for o in broker.orders)
    again = await service.decide_exit(item.id, UserDecision.SELL)
    assert again.status == EXIT_SOLD


@pytest.mark.asyncio
async def test_api_key_required_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("TRAIDO_API_KEY", "secret-test-key")
    monkeypatch.setenv("TRAIDO_BROKER_MOCK", "true")
    monkeypatch.delenv("TRAIDO_AUTH_DISABLED", raising=False)
    from api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/api/v1/opportunities")
        assert denied.status_code == 401
        ok = await client.get(
            "/api/v1/opportunities",
            headers={"X-API-Key": "secret-test-key"},
        )
        assert ok.status_code == 200
    monkeypatch.delenv("TRAIDO_API_KEY", raising=False)
