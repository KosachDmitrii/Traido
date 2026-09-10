"""Fixed Alpaca Paper venue. Previous runtime selections cannot select an adapter."""

import os
from typing import Any, Literal


class BrokerBackendError(ValueError):
    pass


def get_broker_backend() -> Literal["alpaca"]:
    if os.getenv("TRAIDO_BROKER", "alpaca").strip().lower() not in {"", "alpaca", "paper"}:
        raise BrokerBackendError("ALPACA_ONLY: set TRAIDO_BROKER=alpaca")
    return "alpaca"


def broker_backend_payload() -> dict[str, Any]:
    from core.config import get_settings
    from market_data.factory import resolve_alpaca_data_feed

    settings = get_settings()
    return {
        "backend": "alpaca",
        "environment": "paper",
        "market_data_provider": "alpaca",
        "market_data_feed": resolve_alpaca_data_feed(settings),
        "execution_price_source": "alpaca_paper_nbbo_simulation",
        "note": "Alpaca Paper only. IEX quotes do not guarantee the simulated fill price.",
    }
