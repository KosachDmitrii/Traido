"""Market data factory."""

from __future__ import annotations

from core.config import Settings
from core.ports import MarketDataPort


def resolve_alpaca_data_feed(settings: Settings) -> str:
    """Default IEX (free Alpaca data). Set ALPACA_DATA_FEED=sip when subscribed."""
    if settings.alpaca_data_feed:
        return settings.alpaca_data_feed.strip().lower()
    return "iex"


def create_market_data_port(settings: Settings) -> MarketDataPort:
    """Prefer Alpaca when keys present; otherwise fixture provider for local/dev."""
    from market_data.providers.alpaca import AlpacaMarketData
    from market_data.providers.fixture import FixtureMarketData

    if settings.alpaca_api_key and settings.alpaca_api_secret:
        return AlpacaMarketData(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            base_url=settings.alpaca_data_base_url,
            feed=resolve_alpaca_data_feed(settings),
        )
    return FixtureMarketData()
