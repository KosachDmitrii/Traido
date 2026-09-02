"""Sector assessment — separate from FRED macro/regime.

FRED must never invent sector_label/tradable. Production capital path uses this
port (metadata-driven instrument → industry → benchmark ETF).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ConfigDict

from core.schemas import StrictModel


class SectorAssessment(StrictModel):
    """Immutable sector gate input for ApprovalEvidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    sector_label: str | None = None
    industry_label: str | None = None
    benchmark: str | None = None
    provider: str
    source_ts: datetime | None = None
    tradable_long: bool | None = None
    reason_codes: tuple[str, ...] = ()
    fresh: bool = False


# Instrument → (sector_label, industry, benchmark ETF). Extend via config later.
_INSTRUMENT_SECTOR: dict[str, tuple[str, str, str]] = {
    "NEM": ("materials", "gold_miners", "GDX"),
    "GOLD": ("materials", "gold_miners", "GDX"),
    "AEM": ("materials", "gold_miners", "GDX"),
    "FCX": ("materials", "copper", "XLB"),
    "LLY": ("healthcare", "pharma", "XLV"),
    "JNJ": ("healthcare", "pharma", "XLV"),
    "XOM": ("energy", "integrated_oil", "XLE"),
    "CVX": ("energy", "integrated_oil", "XLE"),
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


class SectorAssessmentPort(Protocol):
    async def assess(
        self, symbol: str, *, now: datetime | None = None
    ) -> SectorAssessment: ...


@dataclass
class MetadataSectorAssessment:
    """Production default: map known symbols; unknown → fail-closed (not tradable)."""

    provider: str = "metadata_sector_map@1"

    async def assess(
        self, symbol: str, *, now: datetime | None = None
    ) -> SectorAssessment:
        now = now or datetime.now(UTC)
        sym = symbol.upper()
        mapped = _INSTRUMENT_SECTOR.get(sym)
        if mapped is None:
            return SectorAssessment(
                symbol=sym,
                sector_label=None,
                industry_label=None,
                benchmark=None,
                provider=self.provider,
                source_ts=None,
                tradable_long=None,
                reason_codes=("SECTOR_METADATA_MISSING",),
                fresh=False,
            )
        sector, industry, benchmark = mapped
        return SectorAssessment(
            symbol=sym,
            sector_label=sector,
            industry_label=industry,
            benchmark=benchmark,
            provider=self.provider,
            source_ts=now,
            tradable_long=True,
            reason_codes=("SECTOR_METADATA_OK",),
            fresh=True,
        )


_DEFAULT_PORT: SectorAssessmentPort | None = None


def get_sector_assessment_port() -> SectorAssessmentPort:
    global _DEFAULT_PORT
    if _DEFAULT_PORT is None:
        _DEFAULT_PORT = MetadataSectorAssessment()
    return _DEFAULT_PORT


def set_sector_assessment_port(port: SectorAssessmentPort | None) -> None:
    """Tests: inject a stub. Pass None to restore the production default."""
    global _DEFAULT_PORT
    _DEFAULT_PORT = port
