"""Market data factory."""

from __future__ import annotations

from core.config import Settings
from core.ports import MarketDataPort


def resolve_alpaca_data_feed(settings: Settings) -> str:
    """Respect explicit data feed; Paper development defaults to free IEX."""
    return (
        (settings.alpaca_data_feed or ("iex" if settings.broker_env.value == "paper" else "sip"))
        .strip()
        .lower()
    )


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
