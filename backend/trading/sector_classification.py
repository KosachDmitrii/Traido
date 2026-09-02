"""SectorClassification — static mapping only. Never a trading verdict."""

from __future__ import annotations

from pydantic import ConfigDict

from core.schemas import StrictModel
from trading.sector_policy import CLASSIFICATION_PROVIDER, CLASSIFICATION_VERSION

# Instrument → (sector, industry, benchmark ETF). Classification only.
_INSTRUMENT_SECTOR: dict[str, tuple[str, str, str]] = {
    "NEM": ("materials", "gold_miners", "GDX"),
    "GOLD": ("materials", "gold_miners", "GDX"),
    "AEM": ("materials", "gold_miners", "GDX"),
    "FCX": ("materials", "copper", "XLB"),
    "LLY": ("healthcare", "pharma", "XLV"),
    "JNJ": ("healthcare", "pharma", "XLV"),
    "XOM": ("energy", "integrated_oil", "XLE"),
    "CVX": ("energy", "integrated_oil", "XLE"),
    "OXY": ("energy", "integrated_oil", "XLE"),
    "AAPL": ("technology", "consumer_electronics", "XLK"),
    "MSFT": ("technology", "software", "XLK"),
    "NVDA": ("technology", "semiconductors", "XLK"),
    "TSLA": ("consumer_discretionary", "auto", "XLY"),
    "AMZN": ("consumer_discretionary", "internet_retail", "XLY"),
    "META": ("communication", "internet", "XLC"),
    "GOOGL": ("communication", "internet", "XLC"),
    "GOOG": ("communication", "internet", "XLC"),
    "JPM": ("financials", "banks", "XLF"),
    "GS": ("financials", "banks", "XLF"),
    "MO": ("consumer_staples", "tobacco", "XLP"),
    "PG": ("consumer_staples", "household", "XLP"),
    "BA": ("industrials", "aerospace", "XLI"),
    "CAT": ("industrials", "machinery", "XLI"),
    "NEE": ("utilities", "electric", "XLU"),
    "PLD": ("real_estate", "reit", "XLRE"),
}


class SectorClassification(StrictModel):
    """Static sector facts. Must never carry tradable_long or market regime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    sector: str | None = None
    industry: str | None = None
    benchmark: str | None = None
    classification_provider: str = CLASSIFICATION_PROVIDER
    classification_version: str = CLASSIFICATION_VERSION


def classify_symbol(symbol: str) -> SectorClassification:
    """Map symbol → sector/industry/benchmark. Unknown → empty classification."""
    sym = symbol.upper()
    mapped = _INSTRUMENT_SECTOR.get(sym)
    if mapped is None:
        return SectorClassification(
            symbol=sym,
            sector=None,
            industry=None,
            benchmark=None,
            classification_provider=CLASSIFICATION_PROVIDER,
            classification_version=CLASSIFICATION_VERSION,
        )
    sector, industry, benchmark = mapped
    return SectorClassification(
        symbol=sym,
        sector=sector,
        industry=industry,
        benchmark=benchmark,
        classification_provider=CLASSIFICATION_PROVIDER,
        classification_version=CLASSIFICATION_VERSION,
    )
