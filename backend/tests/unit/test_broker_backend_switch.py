"""The desk cannot select another venue or a live/test endpoint."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.trading import router
from broker.alpaca import AlpacaPaperBroker
from broker.backend_policy import BrokerBackendError, broker_backend_payload
from broker.factory import create_broker
from core.config import Settings


def test_only_alpaca_is_constructed(monkeypatch):
    monkeypatch.delenv("TRAIDO_BROKER_MOCK")
    monkeypatch.setenv("TRAIDO_BROKER", "alpaca")
    broker = create_broker(Settings(ALPACA_API_KEY="test", ALPACA_API_SECRET="secret"))
    assert isinstance(broker, AlpacaPaperBroker)
    assert broker.environment == "paper"
    assert broker_backend_payload()["market_data_feed"] == "iex"


def test_retired_deployment_selection_fails_closed(monkeypatch):
    monkeypatch.delenv("TRAIDO_BROKER_MOCK")
    monkeypatch.setenv("TRAIDO_BROKER", "retired-venue")
    with pytest.raises(BrokerBackendError, match="ALPACA_ONLY"):
        create_broker(Settings(ALPACA_API_KEY="test", ALPACA_API_SECRET="secret"))


@pytest.mark.parametrize("backend", ["alpaca", "retired-venue", "live"])
def test_selection_endpoint_is_gone(backend):
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).put("/api/v1/broker-backend", json={"backend": backend})
    assert response.status_code == 410


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.test",
        "https://evil.test/paper-api",
    ],
)
def test_only_exact_paper_endpoint_is_allowed(url):
    with pytest.raises(RuntimeError):
        AlpacaPaperBroker("key", "secret", url)


def test_mock_is_forbidden_in_production():
    with pytest.raises(RuntimeError, match="MOCK_BROKER"):
        create_broker(Settings(TRAIDO_ENV="production"))


@pytest.mark.asyncio
async def test_submission_server_error_is_unknown_not_rejected():
    from broker.interface import BrokerUnreachable
    from tests.contract.test_broker_contract import _buy

    broker = AlpacaPaperBroker(
        "key",
        "secret",
        "https://paper-api.alpaca.markets",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    with pytest.raises(BrokerUnreachable):
        await broker.place_order(_buy())


@pytest.mark.asyncio
async def test_status_reads_account_before_reporting_ready(monkeypatch):
    from api.routes.trading import get_broker_backend_route
    from tests.alpaca_account import account_broker

    broker = account_broker({"id": "verified-account", "currency": "USD", "equity": "100000"})
    monkeypatch.setattr("broker.factory.create_broker", lambda _: broker)
    result = await get_broker_backend_route()
    assert result["connection_state"] == "ready"
    assert result["account_id"] == "verified-account"
    assert result["market_data_feed"] == "iex"


@pytest.mark.asyncio
async def test_status_does_not_report_ready_when_account_read_fails(monkeypatch):
    from api.routes.trading import get_broker_backend_route

    broker = AlpacaPaperBroker(
        "key",
        "secret",
        "https://paper-api.alpaca.markets",
        transport=httpx.MockTransport(lambda _: httpx.Response(401)),
    )
    broker._cache_clear()
    monkeypatch.setattr("broker.factory.create_broker", lambda _: broker)
    result = await get_broker_backend_route()
    assert result["connection_state"] == "disconnected"
    assert "account_id" not in result
