"""Operator can switch paper execution venue without restarting the process."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import trading as trading_routes
from broker import backend_policy, factory
from broker.backend_policy import (
    BrokerBackendError,
    get_broker_backend,
    normalize_backend,
    reset_broker_backend_cache,
    set_broker_backend,
)
from broker.factory import apply_broker_backend, clear_broker_singleton, create_broker
from broker.switch_guard import broker_switch_blocked_reason
from core.config import Settings
from core.enums import IntentPurpose, IntentStatus, OrderSide, OrderType, PositionStatus
from trading.intents import MemoryOrderIntentStore
from trading.ledger import LEDGER
from trading.order_intent import OrderIntent


@pytest.fixture(autouse=True)
def _isolated_broker_policy(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "broker_backend.json"
    monkeypatch.setattr(backend_policy, "POLICY_PATH", path)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TRAIDO_BROKER", raising=False)
    monkeypatch.delenv("TRAIDO_BROKER_MOCK", raising=False)
    reset_broker_backend_cache()
    clear_broker_singleton()
    yield
    reset_broker_backend_cache()
    clear_broker_singleton()


def test_normalize_rejects_live() -> None:
    with pytest.raises(BrokerBackendError, match="live"):
        normalize_backend("live")


def test_env_bootstrap_ibkr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAIDO_BROKER", "ibkr")
    reset_broker_backend_cache()
    assert get_broker_backend() == "ibkr"


def test_persisted_backend_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAIDO_BROKER", "ibkr")
    set_broker_backend("alpaca", actor="test")
    reset_broker_backend_cache()
    assert get_broker_backend() == "alpaca"


def test_apply_clears_ibkr_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAIDO_BROKER_MOCK", "true")
    settings = Settings(
        alpaca_api_key=None,
        alpaca_api_secret=None,
        finnhub_api_key=None,
        fred_api_key=None,
    )
    set_broker_backend("ibkr", actor="test")
    # Mock path ignores backend — still exercises clear on switch.
    first = create_broker(settings)
    factory._ibkr_broker = first  # type: ignore[attr-defined]
    apply_broker_backend("alpaca", actor="test")
    assert factory._ibkr_broker is None  # type: ignore[attr-defined]
    assert get_broker_backend() == "alpaca"


def test_switch_blocked_with_open_position(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.schemas import Position

    pos = Position(
        id=uuid4(),
        symbol="AAPL",
        qty=Decimal(1),
        avg_entry=Decimal(100),
        stop_price=Decimal(95),
        target_price=Decimal(110),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(UTC),
    )
    monkeypatch.setattr(LEDGER, "get_open", lambda: [pos])
    monkeypatch.setattr("broker.switch_guard.INTENTS.list_unresolved", list)
    assert broker_switch_blocked_reason() is not None
    assert "open_positions" in (broker_switch_blocked_reason() or "")


def test_switch_blocked_with_unknown_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    intents = MemoryOrderIntentStore()
    intents.create_or_get(
        OrderIntent(
            id=uuid4(),
            idempotency_key="test-unknown",
            broker="test",
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(1),
            order_type=OrderType.MARKET,
            purpose=IntentPurpose.ENTRY,
            status=IntentStatus.UNKNOWN,
        )
    )
    monkeypatch.setattr(LEDGER, "get_open", lambda: [])
    monkeypatch.setattr("broker.switch_guard.INTENTS", intents)
    reason = broker_switch_blocked_reason()
    assert reason is not None
    assert "unknown_intents" in reason


def test_put_broker_backend_rejects_when_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(trading_routes.router)
    client = TestClient(app)
    monkeypatch.setattr(
        "broker.switch_guard.broker_switch_blocked_reason",
        lambda: "open_positions:AAPL",
    )

    res = client.put("/api/v1/broker-backend", json={"backend": "ibkr"})
    assert res.status_code == 409
    assert "broker_switch_blocked" in res.json()["detail"]


def test_put_broker_backend_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(trading_routes.router)
    client = TestClient(app)
    monkeypatch.setattr("broker.switch_guard.broker_switch_blocked_reason", lambda: None)

    class _Audit:
        async def append(self, *a, **k):
            return None

    monkeypatch.setattr("api.routes.trading.create_audit", lambda: _Audit())

    res = client.put("/api/v1/broker-backend", json={"backend": "alpaca"})
    assert res.status_code == 200, res.text
    assert res.json()["backend"] == "alpaca"
    assert res.json()["environment"] == "paper"
