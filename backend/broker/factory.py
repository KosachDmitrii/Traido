"""Alpaca Paper execution; in-memory broker requires explicit test opt-in."""

import os

from broker.alpaca import AlpacaPaperBroker
from broker.backend_policy import get_broker_backend
from broker.interface import assert_paper_only
from broker.paper.mock import MockPaperBroker
from core.config import Settings
from core.ports import BrokerPort


class BrokerCredentialsMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__("BROKER_CREDENTIALS_MISSING: set ALPACA_API_KEY and ALPACA_API_SECRET")


def create_broker(settings: Settings) -> BrokerPort:
    assert_paper_only(settings.broker_env)
    if settings.allow_live_trading:
        raise RuntimeError("LIVE_TRADING_DISABLED")
    if os.getenv("TRAIDO_BROKER_MOCK", "").lower() in {"1", "true", "yes"}:
        if settings.environment == "production":
            raise RuntimeError("MOCK_BROKER_FORBIDDEN_IN_PRODUCTION")
        return MockPaperBroker()
    get_broker_backend()  # Reject a stale deployment selector, never silently change venue.
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        return AlpacaPaperBroker(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            base_url=settings.alpaca_broker_base_url,
        )
    raise BrokerCredentialsMissing()
