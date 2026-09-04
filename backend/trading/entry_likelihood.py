"""Entry likelihood — how realistic is zone reach before expiration?

Not probability of profit — only zone-touch realism. No fake % until calibrated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from core.schemas import EntryTimingFacts, EntryWatch
from trading.execution_geometry import resolve_capital_atr


class LikelihoodClass(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class EntryLikelihood:
    __slots__ = (
        "classification",
        "distance_atr",
        "distance_pct",
        "reason_codes",
        "score",
        "time_remaining_minutes",
        "trend_penalty",
        "volatility_support",
    )

    def __init__(
        self,
        *,
        classification: LikelihoodClass,
        score: float,
        distance_pct: float | None,
        distance_atr: float | None,
        volatility_support: float,
        trend_penalty: float,
        time_remaining_minutes: int,
        reason_codes: list[str],
    ) -> None:
        self.classification = classification
        self.score = score
        self.distance_pct = distance_pct
        self.distance_atr = distance_atr
        self.volatility_support = volatility_support
        self.trend_penalty = trend_penalty
        self.time_remaining_minutes = time_remaining_minutes
        self.reason_codes = reason_codes

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "score": round(self.score, 1),
            "distance_pct": round(self.distance_pct, 2) if self.distance_pct is not None else None,
            "distance_atr": round(self.distance_atr, 2) if self.distance_atr is not None else None,
            "volatility_support": round(self.volatility_support, 1),
            "trend_penalty": round(self.trend_penalty, 1),
            "time_remaining_minutes": self.time_remaining_minutes,
            "reason_codes": self.reason_codes,
        }


def _minutes_remaining(watch: EntryWatch, now: datetime) -> int:
    if watch.valid_until.tzinfo is None:
        end = watch.valid_until.replace(tzinfo=UTC)
    else:
        end = watch.valid_until.astimezone(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0, int((end - now).total_seconds() // 60))


def evaluate_entry_likelihood(
    watch: EntryWatch,
    *,
    price: float,
    facts: EntryTimingFacts | None = None,
    now: datetime | None = None,
) -> EntryLikelihood:
    now = now or datetime.now(UTC)
    zone_lo = float(watch.entry_zone_low)
    zone_hi = float(watch.entry_zone_high)
    anchor = (zone_lo + zone_hi) / 2.0
    atr = resolve_capital_atr(
        facts_atr=(facts.atr if facts else None),
        snapshot_atr=(
            watch.admission_snapshot.atr_at_creation if watch.admission_snapshot else None
        ),
    )
    if atr is None:
        return EntryLikelihood(
            classification=LikelihoodClass.LOW,
            score=0.0,
            distance_pct=None,
            distance_atr=None,
            volatility_support=0.0,
            trend_penalty=0.0,
            time_remaining_minutes=_minutes_remaining(watch, now),
            reason_codes=["MISSING_ATR"],
        )

    if zone_lo <= price <= zone_hi:
        return EntryLikelihood(
            classification=LikelihoodClass.HIGH,
            score=95.0,
            distance_pct=0.0,
            distance_atr=0.0,
            volatility_support=80.0,
            trend_penalty=0.0,
            time_remaining_minutes=_minutes_remaining(watch, now),
            reason_codes=["IN_ZONE"],
        )

    if price > zone_hi:
        distance = price - zone_hi
        direction = "above"
    else:
        distance = zone_lo - price
        direction = "below"

    distance_pct = distance / anchor * 100.0
    distance_atr = distance / atr

    reasons: list[str] = [
        f"{distance_atr:.1f} ATR {direction} zone",
        f"{distance_pct:.1f}% from zone",
    ]

    ttl = _minutes_remaining(watch, now)
    if ttl <= 30:
        reasons.append("short TTL")
    elif ttl >= 180:
        reasons.append("ample TTL")

    vol_support = 70.0
    if facts and facts.atr and facts.current_price > 0:
        daily_move_pct = facts.atr / facts.current_price * 100.0
        if daily_move_pct >= 2.5:
            vol_support = 85.0
            reasons.append("high volatility supports reach")
        elif daily_move_pct < 1.0:
            vol_support = 45.0
            reasons.append("low volatility")

    trend_penalty = 0.0
    if direction == "above" and facts:
        if facts.distance_from_fast_ema_pct is not None and facts.distance_from_fast_ema_pct > 2.0:
            trend_penalty += 25.0
            reasons.append("extended above fast EMA")
        if facts.short_term_momentum_pct is not None and facts.short_term_momentum_pct > 1.5:
            trend_penalty += 15.0
            reasons.append("momentum away from zone")

    score = 70.0
    score -= min(45.0, distance_atr * 12.0)
    score -= trend_penalty * 0.4
    score += (vol_support - 50.0) * 0.2
    if ttl >= 120:
        score += 8.0
    elif ttl <= 45:
        score -= 12.0
    score = max(0.0, min(100.0, score))

    if distance_atr <= 0.5 or score >= 72:
        klass = LikelihoodClass.HIGH
    elif distance_atr <= 2.0 and score >= 42:
        klass = LikelihoodClass.MODERATE
    else:
        klass = LikelihoodClass.LOW

    return EntryLikelihood(
        classification=klass,
        score=score,
        distance_pct=distance_pct,
        distance_atr=distance_atr,
        volatility_support=vol_support,
        trend_penalty=trend_penalty,
        time_remaining_minutes=ttl,
        reason_codes=reasons,
    )
