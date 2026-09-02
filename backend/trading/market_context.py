"""Minimal market context — SPY regime + sector ETF relative strength (v1)."""

from __future__ import annotations

from datetime import UTC, datetime

from core.enums import MarketRegimeLabel
from core.schemas import MarketAssessment, StrictModel


class MarketContext(StrictModel):
    market_regime: MarketRegimeLabel | str = MarketRegimeLabel.NEUTRAL
    sector_regime: MarketRegimeLabel | str = MarketRegimeLabel.NEUTRAL
    relative_strength_market: float | None = None
    relative_strength_sector: float | None = None
    sector_etf: str | None = None
    observed_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "market_regime": str(self.market_regime),
            "sector_regime": str(self.sector_regime),
            "relative_strength_market": self.relative_strength_market,
            "relative_strength_sector": self.relative_strength_sector,
            "sector_etf": self.sector_etf,
            "observed_at": self.observed_at.isoformat(),
        }


# Symbol → sector ETF proxy (minimal v1 map).
SECTOR_ETF_MAP: dict[str, str] = {
    "NEM": "GDX",
    "GOLD": "GDX",
    "FCX": "XLB",
    "LLY": "XLV",
    "JNJ": "XLV",
    "XOM": "XLE",
    "AAPL": "XLK",
    "MSFT": "XLK",
    "JPM": "XLF",
}


def sector_etf_for(symbol: str) -> str | None:
    sym = symbol.upper()
    if sym in SECTOR_ETF_MAP:
        return SECTOR_ETF_MAP[sym]
    return None


def build_market_context(
    *,
    symbol: str,
    market: MarketAssessment | None = None,
    symbol_rs_vs_spy: float | None = None,
    sector_rs_vs_spy: float | None = None,
    sector_etf: str | None = None,
    now: datetime | None = None,
) -> MarketContext:
    """Compose context from pipeline market read and optional RS facts."""
    now = now or datetime.now(UTC)
    regime = market.regime if market else MarketRegimeLabel.NEUTRAL
    sector_raw = getattr(market, "sector_regime", None) if market else None
    sector = sector_raw if sector_raw is not None else MarketRegimeLabel.NEUTRAL
    etf = sector_etf or sector_etf_for(symbol)
    rs_market = symbol_rs_vs_spy
    if rs_market is None and market is not None:
        rs_raw = getattr(market, "relative_strength_vs_spy", None)
        if rs_raw is not None:
            rs_market = float(rs_raw)
    rs_sector = sector_rs_vs_spy
    if rs_sector is None and market is not None:
        rs_raw = getattr(market, "relative_strength_vs_sector", None)
        if rs_raw is not None:
            rs_sector = float(rs_raw)
    return MarketContext(
        market_regime=regime,
        sector_regime=sector,
        relative_strength_market=rs_market,
        relative_strength_sector=rs_sector,
        sector_etf=etf,
        observed_at=now,
    )
