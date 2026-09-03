"""Broker factory — Alpaca Paper when keyed, else mock.

Execution backend is chosen by operator policy (Settings) with TRAIDO_BROKER as
bootstrap default. Market data stays on Alpaca regardless.

IBKR is cached process-wide: the Gateway session is stateful and keyed by
client_id, so building a new transport per request exhausts or refuses the
socket. Alpaca stays per-call (stateless HTTP). Switching backends disconnects
IBKR and clears that singleton.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from broker.alpaca import AlpacaPaperBroker
from broker.backend_policy import get_broker_backend
from broker.paper.mock import MockPaperBroker
from core.config import Settings
from core.ports import BrokerPort

logger = logging.getLogger(__name__)

_ibkr_broker: BrokerPort | None = None
_ibkr_lock = threading.Lock()


def create_broker(settings: Settings) -> BrokerPort:
    if os.getenv("TRAIDO_BROKER_MOCK", "").lower() in {"1", "true", "yes"}:
        return MockPaperBroker()

    backend = get_broker_backend()
    if backend == "ibkr":
        global _ibkr_broker
        if _ibkr_broker is not None:
            return _ibkr_broker
        with _ibkr_lock:
            if _ibkr_broker is not None:
                return _ibkr_broker
            from broker.ibkr import IBKRBroker
            from broker.ibkr.config import IBKRTransportConfig
            from broker.ibkr.live_transport import IBKRLiveTransport

            config = IBKRTransportConfig.from_env()
            _ibkr_broker = IBKRBroker(
                IBKRLiveTransport(config),
                environment=config.environment.value,
                account_id=config.account,
            )
            return _ibkr_broker

    if settings.alpaca_api_key and settings.alpaca_api_secret:
        return AlpacaPaperBroker(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            base_url=settings.alpaca_broker_base_url,
        )
    return MockPaperBroker()


def _disconnect_ibkr_sync(broker: BrokerPort) -> None:
    transport = getattr(broker, "_transport", None)
    disconnect = getattr(transport, "disconnect", None)
    if disconnect is None:
        return
    try:
        result = disconnect()
        if asyncio.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)
    except Exception:
        logger.warning("broker factory: IBKR disconnect failed", exc_info=True)


def clear_broker_singleton() -> None:
    """Drop the process-wide IBKR handle after a backend switch."""
    global _ibkr_broker
    with _ibkr_lock:
        old = _ibkr_broker
        _ibkr_broker = None
    if old is not None:
        _disconnect_ibkr_sync(old)


def apply_broker_backend(backend: str, *, actor: str = "user") -> str:
    """Persist backend, tear down the previous IBKR session when the venue changes."""
    from broker.backend_policy import set_broker_backend

    previous = get_broker_backend()
    next_backend = set_broker_backend(backend, actor=actor)
    if previous != next_backend:
        clear_broker_singleton()
    return next_backend
