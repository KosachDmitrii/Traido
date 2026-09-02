"""Market/sector regime gate — fail-closed on approval and publish paths."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from core.enums import DataHealthStatus, MarketRegimeLabel
from core.schemas import MarketAssessment, StrictModel, TradeCandidate
from trading.pipeline import UNTRADABLE_REGIMES, regime_allows_long

MARKET_GATE_VERSION = "market_gate@1"
REGIME_TTL_SECONDS = 300


class MarketGateResult(StrictModel):
    tradable_long: bool
    status: DataHealthStatus
    market_label: str
    sector_label: str | None = None
    sector_tradable: bool | None = None
    evaluated_at: datetime
    regime_ts: datetime | None = None
    source_version: str = MARKET_GATE_VERSION
    reason_codes: list[str] = Field(default_factory=list)
    benchmark: str | None = None


def _blocked(
    *,
    label: str,
    reasons: list[str],
    now: datetime,
    sector_label: str | None = None,
    sector_tradable: bool | None = None,
    regime_ts: datetime | None = None,
    benchmark: str | None = None,
) -> MarketGateResult:
    return MarketGateResult(
        tradable_long=False,
        status=DataHealthStatus.UNHEALTHY,
        market_label=label,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
        evaluated_at=now,
        regime_ts=regime_ts,
        reason_codes=reasons,
        benchmark=benchmark,
    )


def evaluate_market_gate(
    market: MarketAssessment | None,
    *,
    now: datetime | None = None,
    sector_label: str | None = None,
    sector_tradable: bool | None = None,
    require_sector: bool = True,
    ttl_seconds: float = REGIME_TTL_SECONDS,
) -> MarketGateResult:
    """Derive long tradability from a market assessment. Missing → blocked.

    Unknown regime strings must never become NEUTRAL. Stale or untimestamped
    assessments are DATA_BLOCKED (UNHEALTHY).
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)

    if market is None:
        return _blocked(
            label="missing", reasons=["REGIME_MISSING"], now=now, sector_label=sector_label
        )

    regime_ts = getattr(market, "evaluated_at", None) or getattr(market, "as_of", None)
    if regime_ts is None:
        return _blocked(
            label=market.regime.value if market.regime else "unknown",
            reasons=["REGIME_TIMESTAMP_MISSING"],
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            benchmark=getattr(market, "benchmark", None),
        )
    if regime_ts.tzinfo is None:
        return _blocked(
            label=market.regime.value if market.regime else "unknown",
            reasons=["REGIME_TIMESTAMP_INVALID"],
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )
    age = (now - regime_ts.astimezone(UTC)).total_seconds()
    if age < 0:
        return _blocked(
            label=market.regime.value if market.regime else "unknown",
            reasons=["REGIME_TIMESTAMP_FUTURE"],
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )
    if age > ttl_seconds:
        return _blocked(
            label=market.regime.value if market.regime else "unknown",
            reasons=["REGIME_STALE"],
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )

    if market.regime is None:
        return _blocked(
            label="unknown",
            reasons=["REGIME_UNKNOWN"],
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )

    label = market.regime.value
    allowed = regime_allows_long(market)
    if allowed is None:
        return _blocked(
            label=label,
            reasons=["REGIME_MISSING"],
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )

    # sector_label alone never compensates for missing sector_tradable.
    if require_sector and sector_tradable is None:
        return _blocked(
            label=label,
            reasons=["SECTOR_ASSESSMENT_MISSING"],
            now=now,
            sector_label=sector_label,
            sector_tradable=None,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )
    if sector_tradable is False:
        return _blocked(
            label=label,
            reasons=["SECTOR_BLOCKED"],
            now=now,
            sector_label=sector_label,
            sector_tradable=False,
            regime_ts=regime_ts,
            benchmark=getattr(market, "benchmark", None),
        )

    if not allowed or market.regime in UNTRADABLE_REGIMES:
        return MarketGateResult(
            tradable_long=False,
            status=DataHealthStatus.HEALTHY,
            market_label=label,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            evaluated_at=now,
            regime_ts=regime_ts,
            reason_codes=["REGIME_BLOCKED"],
            benchmark=getattr(market, "benchmark", None),
        )
    return MarketGateResult(
        tradable_long=True,
        status=DataHealthStatus.HEALTHY,
        market_label=label,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
        evaluated_at=now,
        regime_ts=regime_ts,
        reason_codes=[],
        benchmark=getattr(market, "benchmark", None),
    )


def parse_regime_label(label: str | None) -> MarketRegimeLabel | None:
    """Parse a regime string. Unknown values return None — never NEUTRAL."""
    if not label:
        return None
    try:
        return MarketRegimeLabel(label)
    except ValueError:
        return None


def evaluate_market_gate_for_candidate(
    candidate: TradeCandidate,
    *,
    now: datetime | None = None,
    market: MarketAssessment | None = None,
    sector_label: str | None = None,
    sector_tradable: bool | None = None,
    require_sector: bool = True,
) -> MarketGateResult:
    """Prefer a fresh MarketAssessment; never invent NEUTRAL from a bad label."""
    now = now or datetime.now(UTC)
    if market is not None:
        return evaluate_market_gate(
            market,
            now=now,
            sector_label=sector_label,
            sector_tradable=sector_tradable,
            require_sector=require_sector,
        )

    label = candidate.market_label
    if not label:
        return _blocked(
            label="missing", reasons=["REGIME_MISSING"], now=now, sector_label=sector_label
        )

    regime = parse_regime_label(label)
    if regime is None:
        return _blocked(
            label=label,
            reasons=["REGIME_UNKNOWN", f"REGIME_UNKNOWN:{label}"],
            now=now,
            sector_label=sector_label,
        )

    # Scan-time label alone has no timestamp — cannot prove freshness.
    return _blocked(
        label=regime.value,
        reasons=["REGIME_TIMESTAMP_MISSING", "REGIME_REQUIRES_FRESH_ASSESSMENT"],
        now=now,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
    )
