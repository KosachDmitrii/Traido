"""Market data factory."""

from __future__ import annotations

from core.config import Settings
from core.ports import MarketDataPort


def create_market_data_port(settings: Settings) -> MarketDataPort:
    """Prefer Alpaca when keys present; otherwise fixture provider for local/dev."""
    from market_data.providers.alpaca import AlpacaMarketData
    from market_data.providers.fixture import FixtureMarketData

    if settings.alpaca_api_key and settings.alpaca_api_secret:
        return AlpacaMarketData(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            base_url=settings.alpaca_data_base_url,
        )
    return FixtureMarketData()
