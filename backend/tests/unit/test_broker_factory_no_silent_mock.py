"""MockPaperBroker is explicit — missing Alpaca keys must not invent a backend."""

from __future__ import annotations

import pytest

from broker.factory import BrokerCredentialsMissing, create_broker
from core.config import Settings


def test_missing_alpaca_keys_refuse_without_explicit_mock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from broker import backend_policy
    from broker.backend_policy import reset_broker_backend_cache

    monkeypatch.setattr(backend_policy, "POLICY_PATH", tmp_path / "broker_backend.json")
    monkeypatch.delenv("TRAIDO_BROKER_MOCK", raising=False)
    monkeypatch.delenv("TRAIDO_BROKER", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    reset_broker_backend_cache()
    settings = Settings(
        alpaca_api_key=None,
        alpaca_api_secret=None,
        finnhub_api_key=None,
        fred_api_key=None,
    )
    with pytest.raises(BrokerCredentialsMissing):
        create_broker(settings)


def test_explicit_mock_flag_still_builds_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    from broker.paper.mock import MockPaperBroker

    monkeypatch.setenv("TRAIDO_BROKER_MOCK", "true")
    settings = Settings(
        alpaca_api_key=None,
        alpaca_api_secret=None,
        finnhub_api_key=None,
        fred_api_key=None,
    )
    broker = create_broker(settings)
    assert isinstance(broker, MockPaperBroker)
