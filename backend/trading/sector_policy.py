"""Versioned sector-assessment policy — TTLs and bar floors live here only."""

from __future__ import annotations

CLASSIFICATION_VERSION = "sector_classification@1"
CLASSIFICATION_PROVIDER = "metadata_sector_map@1"

SECTOR_ASSESSMENT_VERSION = "sector_assessment@1"
SECTOR_MARKET_DATA_PROVIDER = "ohlcv_benchmark@1"

# Daily bars for the sector benchmark ETF (GDX, XLV, …).
BENCHMARK_TIMEFRAME = "1Day"
BENCHMARK_MIN_BARS = 50
"""Minimum bars before regime classify is allowed to set tradable_long True/False."""

BENCHMARK_BAR_TTL_SECONDS = 86_400 * 3
"""Last benchmark bar may be at most this old (calendar weekends → ~3 sessions)."""

BENCHMARK_LOOKBACK_DAYS = 400
"""Fetch window for daily bars (covers SMA200 lookback in market_regime.classify)."""
