"""Broker factory — Alpaca Paper when keyed, else mock.

IBKR is selectable but not the default: the adapter passes the lifecycle
contract suite against a fake transport and has never spoken to an IB Gateway,
so it stays opt-in until that changes.
"""

from __future__ import annotations

import os

from broker.alpaca import AlpacaPaperBroker
from broker.paper.mock import MockPaperBroker
from core.config import Settings
from core.ports import BrokerPort


def create_broker(settings: Settings) -> BrokerPort:
    if os.getenv("TRAIDO_BROKER_MOCK", "").lower() in {"1", "true", "yes"}:
        return MockPaperBroker()

    if os.getenv("TRAIDO_BROKER", "").lower() == "ibkr":
        from broker.ibkr import IBKRBroker

        # No transport is wired, so this refuses on first use rather than
        # silently behaving like a connected broker.
        return IBKRBroker()

    if settings.alpaca_api_key and settings.alpaca_api_secret:
        return AlpacaPaperBroker(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            base_url=settings.alpaca_broker_base_url,
        )
    return MockPaperBroker()
