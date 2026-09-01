"""Stage 2 — deterministic quant pre-ranking of Stage 1 survivors.

Arithmetic on daily bars that were fetched in one batched call. No LLM, no news,
no per-symbol request, no portfolio. The output is an ordering, and the ordering
decides which handful of names are worth the expensive Stage 3 analysis.

Two properties matter more than the formula:

*Deterministic.* Given the same bars, the same order, every time, on every
machine, regardless of which provider response arrived first. Every tie is
broken, and the last tie-break is the symbol, so no two candidates can ever be
genuinely tied. Ranking that depends on completion order is the defect this
whole file exists to make impossible.

*Explainable.* Each component is kept alongside the score. A shortlist nobody
can interrogate is a shortlist nobody can improve, and the operator's first
question about a name they did not expect is always "why that one".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from core.schemas import Bar
from universe.models import Instrument

MIN_BARS = 60
"""Daily bars required before a name may be scored at all.

Below this the averages are noise: a 50-day trend measured on 20 bars is a
20-day trend wearing the wrong label. A new listing therefore waits rather than
being scored optimistically, which is also why new listings are not tradable on
their first day under this design.
"""

MAX_BAR_AGE_DAYS = 5.0
"""How stale the daily series may be before the name is dropped.

A feed that stopped a week ago returns a pass, not an error. The same rule the
execution path applies to bars, applied here, so a dead symbol cannot be ranked
first on a year-old trend.
"""


class PrerankReason:
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    STALE_BARS = "STALE_BARS"
    NO_BARS = "NO_BARS"
    INVALID_BARS = "INVALID_BARS"
    OUTRANKED = "OUTRANKED"


@dataclass(frozen=True)
class PrerankPolicy:
    min_bars: int = MIN_BARS
    max_bar_age_days: float = MAX_BAR_AGE_DAYS
    min_avg_dollar_volume: Decimal = Decimal(10_000_000)
    """Average traded value over the lookback, as opposed to Stage 1's today.

    Both are needed and they catch different things: Stage 1 rejects a normally
    liquid name having a dead session, this rejects a normally dead name having
    one busy session. Neither alone is enough.
    """

    trend_weight: float = 0.35
    momentum_weight: float = 0.30
    relative_volume_weight: float = 0.20
    proximity_weight: float = 0.15


@dataclass
class QuantCandidate:
    """A scored name, with the evidence and the data's own timestamp."""

    symbol: str
    instrument: Instrument
    quant_score: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    data_ts: datetime | None = None
    """Event time of the newest bar used — not when we computed the score."""

    passed: bool = False
    reasons: tuple[str, ...] = ()


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_candidate(
    instrument: Instrument,
    bars: list[Bar],
    policy: PrerankPolicy,
    *,
    now: datetime,
) -> QuantCandidate:
    """Score one name from its daily series. Pure and deterministic."""
    symbol = instrument.key
    if not bars:
        return QuantCandidate(
            symbol=symbol, instrument=instrument, reasons=(PrerankReason.NO_BARS,)
        )
    if len(bars) < policy.min_bars:
        return QuantCandidate(
            symbol=symbol,
            instrument=instrument,
            reasons=(PrerankReason.INSUFFICIENT_HISTORY,),
            features={"bars": float(len(bars))},
        )

    ordered = sorted(bars, key=lambda b: b.ts)
    newest = ordered[-1]
    ts = newest.ts if newest.ts.tzinfo else newest.ts.replace(tzinfo=UTC)
    age_days = (now - ts).total_seconds() / 86400.0
    if age_days > policy.max_bar_age_days:
        return QuantCandidate(
            symbol=symbol,
            instrument=instrument,
            data_ts=ts,
            reasons=(PrerankReason.STALE_BARS,),
            features={"bar_age_days": age_days},
        )

    closes = [float(b.close) for b in ordered]
    volumes = [float(b.volume) for b in ordered]
    highs = [float(b.high) for b in ordered]
    lows = [float(b.low) for b in ordered]

    if any(c <= 0 for c in closes[-policy.min_bars :]):
        return QuantCandidate(
            symbol=symbol,
            instrument=instrument,
            data_ts=ts,
            reasons=(PrerankReason.INVALID_BARS,),
        )

    last = closes[-1]
    sma50 = _sma(closes, 50)
    avg_vol20 = _sma(volumes, 20) or 0.0
    avg_dollar_volume = avg_vol20 * last

    features: dict[str, float] = {
        "bars": float(len(ordered)),
        "close": last,
        "avg_dollar_volume": avg_dollar_volume,
        "bar_age_days": age_days,
    }

    if Decimal(str(avg_dollar_volume)) < policy.min_avg_dollar_volume:
        return QuantCandidate(
            symbol=symbol,
            instrument=instrument,
            data_ts=ts,
            reasons=("INSUFFICIENT_AVG_DOLLAR_VOLUME",),
            features=features,
        )

    # Trend: how far above its own 50-day mean the name is trading, capped so a
    # single parabolic name cannot dominate the shortlist on this term alone.
    trend = 0.0
    if sma50:
        trend = _clamp((last / sma50 - 1.0) / 0.20)
        features["trend_vs_sma50"] = last / sma50 - 1.0

    # Momentum over 20 sessions, on the same bounded scale.
    momentum = 0.0
    if len(closes) >= 21 and closes[-21] > 0:
        raw = last / closes[-21] - 1.0
        momentum = _clamp((raw + 0.10) / 0.30)
        features["momentum_20d"] = raw

    # Relative volume: today against the 20-day mean. Participation, not size.
    relative_volume = 0.0
    if avg_vol20 > 0:
        raw_rv = volumes[-1] / avg_vol20
        relative_volume = _clamp((raw_rv - 0.5) / 1.5)
        features["relative_volume"] = raw_rv

    # Proximity: how near the top of the recent range it sits. High is close to
    # breakout, which is what the strategies downstream are looking for.
    proximity = 0.0
    window_high = max(highs[-60:])
    window_low = min(lows[-60:])
    if window_high > window_low:
        raw_pos = (last - window_low) / (window_high - window_low)
        proximity = _clamp(raw_pos)
        features["range_position"] = raw_pos

    score = (
        policy.trend_weight * trend
        + policy.momentum_weight * momentum
        + policy.relative_volume_weight * relative_volume
        + policy.proximity_weight * proximity
    )
    features["component_trend"] = trend
    features["component_momentum"] = momentum
    features["component_relative_volume"] = relative_volume
    features["component_proximity"] = proximity

    return QuantCandidate(
        symbol=symbol,
        instrument=instrument,
        quant_score=round(score, 6),
        features=features,
        data_ts=ts,
        passed=True,
    )


@dataclass
class PrerankOutcome:
    shortlist: list[QuantCandidate] = field(default_factory=list)
    outranked: list[QuantCandidate] = field(default_factory=list)
    rejected: list[QuantCandidate] = field(default_factory=list)

    @property
    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in self.rejected:
            for reason in candidate.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return counts


def prerank(
    instruments: list[Instrument],
    bars_by_symbol: dict[str, list[Bar]],
    *,
    policy: PrerankPolicy | None = None,
    top_k: int = 30,
    now: datetime | None = None,
) -> PrerankOutcome:
    """Score everything, then keep the best `top_k`, deterministically.

    The score is rounded before comparison so two candidates that differ in the
    fifteenth decimal place tie and are separated by symbol instead — otherwise
    floating-point noise, which is not stable across platforms, would decide the
    shortlist and the "identical ranking every run" property would be a
    coincidence rather than a guarantee.
    """
    pol = policy or PrerankPolicy()
    when = now or datetime.now(UTC)
    outcome = PrerankOutcome()

    scored: list[QuantCandidate] = []
    for instrument in instruments:
        candidate = score_candidate(
            instrument, bars_by_symbol.get(instrument.key, []), pol, now=when
        )
        if candidate.passed:
            scored.append(candidate)
        else:
            outcome.rejected.append(candidate)

    scored.sort(key=lambda c: (-c.quant_score, c.symbol))
    if top_k > 0:
        outcome.shortlist = scored[:top_k]
        for cut in scored[top_k:]:
            cut.passed = False
            cut.reasons = (*cut.reasons, PrerankReason.OUTRANKED)
            outcome.outranked.append(cut)
    else:
        outcome.shortlist = scored
    return outcome
