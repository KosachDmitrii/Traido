"""Sector market assessment — real benchmark bars, never static-map tradable.

Classification (NEM→GDX) is separate from assessment (GDX regime from bars).
Static mapping alone must never set tradable_long=True.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import ConfigDict

from core.enums import DataHealthStatus, MarketRegimeLabel, Timeframe
from core.ports import MarketDataPort
from core.schemas import Bar, StrictModel
from quant.market_regime import classify as classify_regime
from trading.sector_classification import SectorClassification, classify_symbol
from trading.sector_policy import (
    BENCHMARK_BAR_TTL_SECONDS,
    BENCHMARK_LOOKBACK_DAYS,
    BENCHMARK_MIN_BARS,
    BENCHMARK_TIMEFRAME,
    SECTOR_ASSESSMENT_VERSION,
    SECTOR_MARKET_DATA_PROVIDER,
)


class SectorMarketAssessment(StrictModel):
    """Benchmark-regime assessment used by Risk Engine and Final Admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    sector: str | None = None
    industry: str | None = None
    benchmark: str | None = None
    benchmark_timeframe: str = BENCHMARK_TIMEFRAME
    benchmark_bars_count: int = 0
    benchmark_last_bar_ts: datetime | None = None
    evaluated_at: datetime
    data_status: DataHealthStatus = DataHealthStatus.UNHEALTHY
    sector_regime: MarketRegimeLabel | None = None
    tradable_long: bool | None = None
    reason_codes: tuple[str, ...] = ()
    market_data_provider: str = SECTOR_MARKET_DATA_PROVIDER
    assessment_version: str = SECTOR_ASSESSMENT_VERSION
    rs_outperformance_21d: float | None = None
    rs_new_high: bool | None = None
    classification_provider: str | None = None
    classification_version: str | None = None

    @property
    def sector_label(self) -> str | None:
        return self.sector

    @property
    def industry_label(self) -> str | None:
        return self.industry

    @property
    def provider(self) -> str:
        return self.market_data_provider

    @property
    def source_ts(self) -> datetime | None:
        return self.benchmark_last_bar_ts

    @property
    def fresh(self) -> bool:
        return self.data_status is DataHealthStatus.HEALTHY and self.tradable_long is not None


# Backward-compatible alias used by older call sites / tests.
SectorAssessment = SectorMarketAssessment


def _blocked(
    *,
    classification: SectorClassification,
    evaluated_at: datetime,
    reasons: tuple[str, ...],
    bars_count: int = 0,
    last_bar_ts: datetime | None = None,
    regime: MarketRegimeLabel | None = None,
) -> SectorMarketAssessment:
    return SectorMarketAssessment(
        symbol=classification.symbol,
        sector=classification.sector,
        industry=classification.industry,
        benchmark=classification.benchmark,
        benchmark_timeframe=BENCHMARK_TIMEFRAME,
        benchmark_bars_count=bars_count,
        benchmark_last_bar_ts=last_bar_ts,
        evaluated_at=evaluated_at,
        data_status=DataHealthStatus.UNHEALTHY,
        sector_regime=regime,
        tradable_long=None,
        reason_codes=reasons,
        market_data_provider=SECTOR_MARKET_DATA_PROVIDER,
        assessment_version=SECTOR_ASSESSMENT_VERSION,
        classification_provider=classification.classification_provider,
        classification_version=classification.classification_version,
    )


def _last_bar_ts(bars: list[Bar]) -> datetime | None:
    if not bars:
        return None
    ts = bars[-1].ts
    if ts is None:
        return None
    if ts.tzinfo is None:
        return None  # naive → invalid; caller treats as DATA_BLOCKED
    return ts.astimezone(UTC)


def assess_from_benchmark_bars(
    classification: SectorClassification,
    bars: list[Bar] | None,
    *,
    now: datetime | None = None,
    symbol_bars: list[Bar] | None = None,
) -> SectorMarketAssessment:
    """Derive tradable_long from real benchmark bars. Never from the static map."""
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    if classification.benchmark is None or classification.sector is None:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_METADATA_MISSING", "SECTOR_ASSESSMENT_MISSING"),
        )

    if bars is None:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_BENCHMARK_UNAVAILABLE", "SECTOR_ASSESSMENT_MISSING"),
        )

    bars_count = len(bars)
    last_ts = _last_bar_ts(bars)
    if bars_count == 0:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_BENCHMARK_BARS_MISSING", "SECTOR_ASSESSMENT_MISSING"),
            bars_count=0,
        )
    if last_ts is None:
        # Missing or naive timestamp
        raw = bars[-1].ts if bars else None
        reason = (
            "SECTOR_BENCHMARK_TIMESTAMP_INVALID"
            if raw is not None and raw.tzinfo is None
            else "SECTOR_BENCHMARK_TIMESTAMP_MISSING"
        )
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=(reason, "SECTOR_ASSESSMENT_MISSING"),
            bars_count=bars_count,
            last_bar_ts=None,
        )
    age = (evaluated_at - last_ts).total_seconds()
    # Sub-second clock skew between assess(now=) and bar provider is not FUTURE.
    if age < -2.0:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_BENCHMARK_TIMESTAMP_FUTURE", "SECTOR_ASSESSMENT_MISSING"),
            bars_count=bars_count,
            last_bar_ts=last_ts,
        )
    if age < 0:
        age = 0.0
    if age > BENCHMARK_BAR_TTL_SECONDS:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_BENCHMARK_STALE", "SECTOR_ASSESSMENT_MISSING"),
            bars_count=bars_count,
            last_bar_ts=last_ts,
        )
    if bars_count < BENCHMARK_MIN_BARS:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_BENCHMARK_INSUFFICIENT_BARS", "SECTOR_ASSESSMENT_MISSING"),
            bars_count=bars_count,
            last_bar_ts=last_ts,
        )

    snap = classify_regime(bars)
    if snap.reasons and "Not enough bars" in snap.reasons[0]:
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_BENCHMARK_INSUFFICIENT_BARS", "SECTOR_ASSESSMENT_MISSING"),
            bars_count=bars_count,
            last_bar_ts=last_ts,
            regime=snap.label,
        )

    tradable = bool(snap.is_tradable_long)
    reasons: list[str] = []
    if tradable:
        reasons.append("SECTOR_BENCHMARK_OK")
    else:
        reasons.append("SECTOR_BLOCKED")
        reasons.append(f"SECTOR_REGIME:{snap.label.value}")

    rs_out: float | None = None
    rs_high: bool | None = None
    if symbol_bars and len(symbol_bars) >= 25 and bars_count >= 25:
        from quant.relative_strength import compute_relative_strength

        rs = compute_relative_strength(
            symbol_bars,
            bars,
            benchmark=classification.benchmark or "SPY",
        )
        rs_out = rs.outperformance_pct.get(21)
        rs_high = rs.rs_new_high
        reasons.extend(rs.reasons[:2])

    return SectorMarketAssessment(
        symbol=classification.symbol,
        sector=classification.sector,
        industry=classification.industry,
        benchmark=classification.benchmark,
        benchmark_timeframe=BENCHMARK_TIMEFRAME,
        benchmark_bars_count=bars_count,
        benchmark_last_bar_ts=last_ts,
        evaluated_at=evaluated_at,
        data_status=DataHealthStatus.HEALTHY,
        sector_regime=snap.label,
        tradable_long=tradable,
        reason_codes=tuple(reasons),
        market_data_provider=SECTOR_MARKET_DATA_PROVIDER,
        assessment_version=SECTOR_ASSESSMENT_VERSION,
        rs_outperformance_21d=rs_out,
        rs_new_high=rs_high,
        classification_provider=classification.classification_provider,
        classification_version=classification.classification_version,
    )


class SectorAssessmentPort(Protocol):
    async def assess(
        self,
        symbol: str,
        *,
        market_data: MarketDataPort | None = None,
        symbol_bars: list[Bar] | None = None,
        now: datetime | None = None,
    ) -> SectorMarketAssessment: ...


@dataclass
class BenchmarkBarsSectorAssessment:
    """Production capital-path assessor: classification + live benchmark bars."""

    provider: str = SECTOR_MARKET_DATA_PROVIDER

    async def assess(
        self,
        symbol: str,
        *,
        market_data: MarketDataPort | None = None,
        symbol_bars: list[Bar] | None = None,
        now: datetime | None = None,
    ) -> SectorMarketAssessment:
        evaluated_at = now or datetime.now(UTC)
        if evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=UTC)
        else:
            evaluated_at = evaluated_at.astimezone(UTC)

        classification = classify_symbol(symbol)
        if classification.benchmark is None:
            return _blocked(
                classification=classification,
                evaluated_at=evaluated_at,
                reasons=("SECTOR_METADATA_MISSING", "SECTOR_ASSESSMENT_MISSING"),
            )
        if market_data is None:
            return _blocked(
                classification=classification,
                evaluated_at=evaluated_at,
                reasons=("SECTOR_MARKET_DATA_NOT_CONFIGURED", "SECTOR_ASSESSMENT_MISSING"),
            )

        end = evaluated_at
        start = end - timedelta(days=BENCHMARK_LOOKBACK_DAYS)
        try:
            bars = await market_data.get_bars(
                classification.benchmark,
                Timeframe.D1,
                start,
                end,
            )
        except Exception:  # noqa: BLE001 — vendor/transport failure → DATA_BLOCKED
            return _blocked(
                classification=classification,
                evaluated_at=evaluated_at,
                reasons=("SECTOR_BENCHMARK_UNAVAILABLE", "SECTOR_ASSESSMENT_MISSING"),
            )

        return assess_from_benchmark_bars(
            classification,
            bars,
            now=evaluated_at,
            symbol_bars=symbol_bars,
        )


# Deprecated name — classification-only stub that never grants tradable_long.
@dataclass
class MetadataSectorAssessment:
    """Classification wrapper. Does NOT set tradable_long from the static map."""

    provider: str = "metadata_sector_map@1"

    async def assess(
        self,
        symbol: str,
        *,
        market_data: MarketDataPort | None = None,
        symbol_bars: list[Bar] | None = None,
        now: datetime | None = None,
    ) -> SectorMarketAssessment:
        # If market_data is provided, behave like the production assessor.
        if market_data is not None:
            return await BenchmarkBarsSectorAssessment(provider=self.provider).assess(
                symbol,
                market_data=market_data,
                symbol_bars=symbol_bars,
                now=now,
            )
        evaluated_at = now or datetime.now(UTC)
        if evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=UTC)
        classification = classify_symbol(symbol)
        if classification.benchmark is None:
            return _blocked(
                classification=classification,
                evaluated_at=evaluated_at,
                reasons=("SECTOR_METADATA_MISSING", "SECTOR_ASSESSMENT_MISSING"),
            )
        # Static map alone → DATA_BLOCKED (tradable_long=None), never True.
        return _blocked(
            classification=classification,
            evaluated_at=evaluated_at,
            reasons=("SECTOR_ASSESSMENT_REQUIRES_BARS", "SECTOR_ASSESSMENT_MISSING"),
        )


_DEFAULT_PORT: SectorAssessmentPort | None = None


def get_sector_assessment_port() -> SectorAssessmentPort:
    global _DEFAULT_PORT
    if _DEFAULT_PORT is None:
        _DEFAULT_PORT = BenchmarkBarsSectorAssessment()
    return _DEFAULT_PORT


def set_sector_assessment_port(port: SectorAssessmentPort | None) -> None:
    """Tests: inject a stub. Pass None to restore the production default."""
    global _DEFAULT_PORT
    _DEFAULT_PORT = port
