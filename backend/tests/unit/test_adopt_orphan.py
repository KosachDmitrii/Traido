"""Adopt orphan positions into the ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from core.enums import (
    IntentPurpose,
    IntentStatus,
    OpportunityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    TradingMode,
)
from core.schemas import OrderRecord, Position
from database.models.desk import OpportunityRow
from trading.adopt_orphan import adopt_orphan_position, clear_orphan_blocks
from trading.intents import INTENTS, OrderIntentStore
from trading.ledger import LEDGER
from trading.order_intent import OrderIntent


@pytest.mark.asyncio
async def test_adopt_orphan_writes_ledger_from_broker_and_card(monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine

    db = tmp_path / "adopt.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    from database.base import Base

    Base.metadata.create_all(engine)

    ledger = type(LEDGER)(engine)
    intents = OrderIntentStore(engine)

    opp_id = uuid4()
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(
            OpportunityRow(
                id=opp_id,
                status=OpportunityStatus.EXECUTED.value,
                trading_mode=TradingMode.CONFIRMATION.value,
                symbol="LLY",
                created_at=datetime.now(UTC),
                expires_at=None,
                payload={
                    "id": str(opp_id),
                    "candidate": {
                        "symbol": "LLY",
                        "action": "buy",
                        "confidence": 0.7,
                        "entry": "1166.76",
                        "stop": "1153.26",
                        "target": "1193.77",
                        "risk_reward": 2.0,
                        "reasons": ["test setup"],
                        "strategy_version": "strategy_confluence@0.3.0-f3",
                        "pipeline_run_id": str(uuid4()),
                    },
                    "risk": {"verdict": "pass", "reasons": ["RISK_OK"], "sized_qty": "4"},
                    "status": "executed",
                    "trading_mode": "confirmation",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()

    stop_oid = "stop-broker-1"
    entry_oid = "entry-broker-1"
    admission_id = uuid4()
    intents.create_or_get(
        OrderIntent(
            idempotency_key=f"entry:{opp_id}:0",
            broker="AlpacaPaperBroker",
            symbol="LLY",
            side=OrderSide.BUY,
            requested_qty=Decimal(4),
            order_type=OrderType.LIMIT,
            purpose=IntentPurpose.ENTRY,
            status=IntentStatus.FILLED,
            broker_order_id=entry_oid,
            opportunity_id=opp_id,
            approval_admission_record_id=admission_id,
            geometry_hash="adopt-test-geo",
            filled_qty=Decimal(4),
            created_at=datetime.now(UTC),
        )
    )
    intents.create_or_get(
        OrderIntent(
            idempotency_key="protection:LLY:0",
            broker="AlpacaPaperBroker",
            symbol="LLY",
            side=OrderSide.SELL,
            requested_qty=Decimal(4),
            order_type=OrderType.STOP,
            purpose=IntentPurpose.PROTECTIVE_EXIT,
            status=IntentStatus.ACKNOWLEDGED,
            broker_order_id=stop_oid,
            created_at=datetime.now(UTC),
        )
    )

    class _Broker:
        async def list_positions(self):
            return [
                Position(
                    id=uuid4(),
                    symbol="LLY",
                    qty=Decimal(4),
                    avg_entry=Decimal("1168.47"),
                    status=PositionStatus.OPEN,
                    opened_at=datetime.now(UTC),
                )
            ]

        async def list_open_orders(self):
            return [
                OrderRecord(
                    id=uuid4(),
                    client_order_id="traido-stop",
                    broker_order_id=stop_oid,
                    symbol="LLY",
                    side=OrderSide.SELL,
                    order_type=OrderType.STOP,
                    qty=Decimal(4),
                    status=OrderStatus.ACCEPTED,
                    stop_price=Decimal("1153.26"),
                )
            ]

    # Without correlation IDs, adoption must refuse (no "latest card by symbol").
    refused = await adopt_orphan_position(
        symbol="LLY",
        broker=_Broker(),
        ledger=ledger,
        intents=intents,
    )
    assert refused["status"] == "error"
    assert refused["reason"] == "correlation_required"

    result = await adopt_orphan_position(
        symbol="LLY",
        broker=_Broker(),
        ledger=ledger,
        intents=intents,
        expected_broker_order_id=entry_oid,
    )
    assert result["status"] == "adopted"
    row = ledger.find_open_by_symbol("LLY")
    assert row is not None
    assert row.target_price == Decimal("1193.77")
    assert row.stop_price == Decimal("1153.26")
    assert row.payload.get("stop_order_id") == stop_oid


def test_clear_orphan_blocks() -> None:
    cleared = clear_orphan_blocks(INTENTS, "LLY")
    assert cleared >= 0
