"""Entry-side data freshness gate — technical failure, not a trading verdict."""

from __future__ import annotations

from datetime import UTC, datetime

from core.enums import DataHealthStatus
from core.schemas import Bar, DataIntegrityResult, Quote

# Bid/ask quotes older than this are not safe for capital-path admission.
QUOTE_MAX_AGE_SEC = 15.0
# Vendor clocks may lead ours by a second — do not DATA_BLOCK on sub-second future skew.
QUOTE_FUTURE_TOLERANCE_SEC = 3.0
MIN_BARS_FOR_FEATURES = 30
# H1 bars older than one full session relative to evaluation are stale.
BARS_MAX_AGE_SEC = 6 * 3600.0


def _aware_utc(ts: datetime) -> datetime | None:
    """Return tz-aware UTC, or None when comparison is impossible."""
    if ts.tzinfo is None:
        return None
    return ts.astimezone(UTC)


def check_data_integrity(
    *,
    quote: Quote | None = None,
    bars_count: int | None = None,
    last_bar_ts: datetime | None = None,
    now: datetime | None = None,
    quote_max_age_sec: float = QUOTE_MAX_AGE_SEC,
    bars_max_age_sec: float = BARS_MAX_AGE_SEC,
    require_bars: bool = False,
) -> DataIntegrityResult:
    """Return HEALTHY / DEGRADED / UNHEALTHY for TradeAdmission.

    Age is always ``evaluated_at - quote.ts``. Never substitute quote.ts for now.
    Future timestamps, timezone-naive timestamps, or incomparable clocks → UNHEALTHY.

    When ``require_bars`` is True (capital path), missing bars_count is UNHEALTHY.
    When False, bars_count=None skips the bars completeness check (scan-time paths).
    """
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    reasons: list[str] = []
    quote_fresh = True
    spread_fresh = True
    last_trade_fresh = True
    bars_complete = True
    timestamps_aligned = True

    if quote is None:
        quote_fresh = False
        spread_fresh = False
        reasons.append("MARKET_DATA_UNHEALTHY")
    else:
        ts = _aware_utc(quote.ts)
        if ts is None:
            quote_fresh = False
            timestamps_aligned = False
            reasons.append("QUOTE_TIMESTAMP_INVALID")
            reasons.append("STALE_DATA")
        else:
            age = (evaluated_at - ts).total_seconds()
            if age < -QUOTE_FUTURE_TOLERANCE_SEC:
                quote_fresh = False
                timestamps_aligned = False
                reasons.append("QUOTE_TIMESTAMP_FUTURE")
                reasons.append("STALE_DATA")
            elif age > quote_max_age_sec:
                quote_fresh = False
                reasons.append("STALE_DATA")
        bid = float(quote.bid or 0)
        ask = float(quote.ask or 0)
        if bid <= 0 or ask < bid:
            spread_fresh = False
            if "MARKET_DATA_UNHEALTHY" not in reasons:
                reasons.append("MARKET_DATA_UNHEALTHY")

    if require_bars and bars_count is None:
        bars_complete = False
        reasons.append("BARS_REQUIRED")
    elif bars_count is not None and bars_count < MIN_BARS_FOR_FEATURES:
        bars_complete = False
        reasons.append("INSUFFICIENT_BARS")

    if last_bar_ts is not None:
        bar_ts = _aware_utc(last_bar_ts)
        if bar_ts is None:
            bars_complete = False
            timestamps_aligned = False
            reasons.append("BAR_TIMESTAMP_INVALID")
        else:
            bar_age = (evaluated_at - bar_ts).total_seconds()
            if bar_age < 0:
                bars_complete = False
                timestamps_aligned = False
                reasons.append("BAR_TIMESTAMP_FUTURE")
            elif bar_age > bars_max_age_sec:
                bars_complete = False
                reasons.append("STALE_BARS")
    elif require_bars and bars_count is not None and bars_count > 0:
        bars_complete = False
        reasons.append("BAR_TIMESTAMP_MISSING")

    if not quote_fresh or not spread_fresh or not timestamps_aligned or not bars_complete:
        status = DataHealthStatus.UNHEALTHY
    else:
        status = DataHealthStatus.HEALTHY

    return DataIntegrityResult(
        status=status,
        last_trade_fresh=last_trade_fresh,
        quote_fresh=quote_fresh,
        spread_fresh=spread_fresh,
        bars_complete=bars_complete,
        timestamps_aligned=timestamps_aligned,
        reason_codes=reasons,
    )


def last_bar_timestamp(bars: list[Bar] | None) -> datetime | None:
    if not bars:
        return None
    return bars[-1].ts
