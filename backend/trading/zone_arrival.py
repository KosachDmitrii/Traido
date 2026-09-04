"""Zone arrival quality — how did price reach the entry zone?

Path-dependent: same final price ≠ same entry quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from core.enums import SetupType
from core.schemas import Bar, EntryWatch
from trading.execution_geometry import resolve_capital_atr

# Overnight session gap (prev close → next open) at or above this pct → GAP_DOWN.
GAP_DOWN_MIN_PCT = 1.5
# Only inspect the recent path into the zone — not every gap in a 25-bar window.
GAP_LOOKBACK_MIN_BARS = 6


def _exchange_date(ts: datetime) -> date:
    from core.clock import ET

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(ET).date()


def _path_origin(watch: EntryWatch) -> float:
    """Measure decline from the signal, not the last WAIT refresh mark.

    Each scanner pass refreshes ``admission_snapshot.price_at_creation`` to the
    current quote. By the time price tags the zone that origin equals the mark,
    so ``move <= 0`` and every in-zone card looked like UNKNOWN/55 — blocked.
    """
    try:
        signal = float(watch.signal_price)
    except (TypeError, ValueError):
        signal = 0.0
    snap = watch.admission_snapshot
    creation = float(snap.price_at_creation) if snap else float(watch.current_price_at_creation)
    if signal > 0:
        return max(signal, creation)
    return creation


class ArrivalType(StrEnum):
    HEALTHY_PULLBACK = "HEALTHY_PULLBACK"
    NORMAL_PULLBACK = "NORMAL_PULLBACK"
    FAST_PULLBACK = "FAST_PULLBACK"
    SELL_OFF = "SELL_OFF"
    CRASH = "CRASH"
    GAP_DOWN = "GAP_DOWN"
    STRUCTURAL_BREAK = "STRUCTURAL_BREAK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ZoneArrivalFacts:
    score: float
    arrival_type: ArrivalType
    arrival_speed_pct: float | None
    arrival_speed_atr: float | None
    atr_velocity: float | None
    bars_to_zone: int | None
    red_bar_ratio: float | None
    consecutive_red_bars: int
    largest_red_bar_atr: float | None
    sell_volume_ratio: float | None
    volume_acceleration: float | None
    gap_down_pct: float | None
    crash_velocity: bool
    structural_damage: bool
    reason_codes: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 1),
            "arrival_type": self.arrival_type.value,
            "arrival_speed_pct": self.arrival_speed_pct,
            "arrival_speed_atr": self.arrival_speed_atr,
            "atr_velocity": self.atr_velocity,
            "bars_to_zone": self.bars_to_zone,
            "red_bar_ratio": self.red_bar_ratio,
            "consecutive_red_bars": self.consecutive_red_bars,
            "largest_red_bar_atr": self.largest_red_bar_atr,
            "sell_volume_ratio": self.sell_volume_ratio,
            "volume_acceleration": self.volume_acceleration,
            "gap_down_pct": self.gap_down_pct,
            "crash_velocity": self.crash_velocity,
            "structural_damage": self.structural_damage,
            "reason_codes": self.reason_codes,
        }


# Configurable crash detector defaults (Phase 2 v1).
CRASH_DECLINE_ATR = 1.8
CRASH_MAX_BARS = 3
CRASH_VOLUME_MULT = 1.8

ARRIVAL_MODEL_VERSION = "arrival@1"


def zone_arrival_required(setup_type: SetupType) -> bool:
    return setup_type in {
        SetupType.PULLBACK_CONTINUATION,
        SetupType.VWAP_RECLAIM,
        SetupType.MEAN_REVERSION,
    }


def detect_crash_velocity(
    *,
    decline_atr: float,
    bars: int,
    volume_ratio: float | None,
) -> bool:
    return bool(
        decline_atr >= CRASH_DECLINE_ATR
        and bars <= CRASH_MAX_BARS
        and (volume_ratio is None or volume_ratio >= CRASH_VOLUME_MULT)
    )


def evaluate_zone_arrival(
    watch: EntryWatch,
    bars: list[Bar],
    *,
    atr: float | None = None,
    current_price: float | None = None,
) -> ZoneArrivalFacts:
    """Analyze path from creation (or recent window) into the zone."""
    price = current_price
    if price is None and watch.last_price is not None:
        price = float(watch.last_price)
    if price is None:
        price = float(watch.current_price_at_creation)

    snap = watch.admission_snapshot
    origin = _path_origin(watch)
    atr_f = resolve_capital_atr(
        facts_atr=atr,
        snapshot_atr=(snap.atr_at_creation if snap else None),
    )

    if len(bars) < 5:
        return ZoneArrivalFacts(
            score=0.0,
            arrival_type=ArrivalType.UNKNOWN,
            arrival_speed_pct=None,
            arrival_speed_atr=None,
            atr_velocity=None,
            bars_to_zone=None,
            red_bar_ratio=None,
            consecutive_red_bars=0,
            largest_red_bar_atr=None,
            sell_volume_ratio=None,
            volume_acceleration=None,
            gap_down_pct=None,
            crash_velocity=False,
            structural_damage=False,
            reason_codes=["INSUFFICIENT_BARS"],
        )

    if atr_f is None:
        return ZoneArrivalFacts(
            score=0.0,
            arrival_type=ArrivalType.UNKNOWN,
            arrival_speed_pct=None,
            arrival_speed_atr=None,
            atr_velocity=None,
            bars_to_zone=None,
            red_bar_ratio=None,
            consecutive_red_bars=0,
            largest_red_bar_atr=None,
            sell_volume_ratio=None,
            volume_acceleration=None,
            gap_down_pct=None,
            crash_velocity=False,
            structural_damage=False,
            reason_codes=["MISSING_ATR", "INSUFFICIENT_BARS"],
        )

    window = bars[-min(25, len(bars)) :]
    closes = [float(b.close) for b in window]
    volumes = [float(b.volume) for b in window]

    move = origin - price
    arrival_speed_atr = abs(move) / atr_f if atr_f > 0 else None
    arrival_speed_pct = abs(move) / origin * 100.0 if origin > 0 else None

    red_count = 0
    consecutive_red = 0
    max_consecutive_red = 0
    largest_red_atr = 0.0
    for b in window:
        o, c = float(b.open), float(b.close)
        if c < o:
            red_count += 1
            consecutive_red += 1
            max_consecutive_red = max(max_consecutive_red, consecutive_red)
            body = o - c
            largest_red_atr = max(largest_red_atr, body / atr_f if atr_f > 0 else 0)
        else:
            consecutive_red = 0

    red_ratio = red_count / len(window)
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    recent_vol = sum(volumes[-3:]) / min(3, len(volumes)) if volumes else 0
    vol_accel = recent_vol / avg_vol if avg_vol > 0 else 1.0

    early_vol = sum(volumes[: max(1, len(volumes) // 2)]) / max(1, len(volumes) // 2)
    late_vol = sum(volumes[len(volumes) // 2 :]) / max(1, len(volumes) - len(volumes) // 2)
    sell_vol_ratio = late_vol / early_vol if early_vol > 0 else 1.0

    bars_to_zone = None
    for i, c in enumerate(reversed(closes)):
        if float(watch.entry_zone_low) <= c <= float(watch.entry_zone_high):
            bars_to_zone = i + 1
            break

    gap_down = None
    # H1 open vs prior close jumps intraday are not overnight gaps — only count
    # session boundaries (Fri→Mon, prior close→RTH open). Scan the recent approach
    # tail only so a gap days ago does not block every in-zone card.
    gap_lookback = min(len(window), max(bars_to_zone or 0, GAP_LOOKBACK_MIN_BARS) + 2)
    gap_window = window[-gap_lookback:]
    for i in range(1, len(gap_window)):
        prev_bar = gap_window[i - 1]
        curr_bar = gap_window[i]
        if _exchange_date(prev_bar.ts) == _exchange_date(curr_bar.ts):
            continue
        prev_close = float(prev_bar.close)
        curr_open = float(curr_bar.open)
        if prev_close <= 0:
            continue
        gap_pct = (curr_open - prev_close) / prev_close * 100.0
        if gap_pct <= -GAP_DOWN_MIN_PCT:
            gap_down = abs(gap_pct)
            break

    bars_elapsed = max(bars_to_zone or len(window), 1)
    atr_velocity = arrival_speed_atr / bars_elapsed if arrival_speed_atr is not None else None

    crash = detect_crash_velocity(
        decline_atr=arrival_speed_atr or 0.0,
        bars=bars_elapsed,
        volume_ratio=sell_vol_ratio,
    )

    reasons: list[str] = []
    score = 75.0

    if move > 0:
        if vol_accel <= 0.85 and red_ratio <= 0.45:
            arrival = ArrivalType.HEALTHY_PULLBACK
            score = 82.0
            reasons.extend(["HEALTHY_VOLUME_CONTRACTION", "ORDERLY_RETRACEMENT"])
        elif arrival_speed_atr is not None and arrival_speed_atr <= 1.2:
            arrival = ArrivalType.NORMAL_PULLBACK
            score = 68.0
        elif arrival_speed_atr is not None and arrival_speed_atr <= 2.0:
            arrival = ArrivalType.FAST_PULLBACK
            score = 48.0
            reasons.append("FAST_DECLINE")
        else:
            arrival = ArrivalType.SELL_OFF
            score = 28.0
            reasons.extend(["FAST_DECLINE", "SELL_VOLUME_ACCELERATING"])
    else:
        lo = float(watch.entry_zone_low)
        hi = float(watch.entry_zone_high)
        if lo <= price <= hi:
            arrival = ArrivalType.NORMAL_PULLBACK
            score = 68.0
            reasons.append("IN_ZONE_ORDERLY")
        else:
            arrival = ArrivalType.UNKNOWN
            score = 32.0
            reasons.append("NO_PULLBACK_PATH")

    if crash:
        arrival = ArrivalType.CRASH
        score = min(score, 18.0)
        reasons.append("CRASH_VELOCITY")

    if gap_down is not None and gap_down >= GAP_DOWN_MIN_PCT:
        arrival = ArrivalType.GAP_DOWN
        score = min(score, 22.0)
        reasons.append("GAP_DOWN")

    if vol_accel >= 1.4 and red_ratio >= 0.55:
        score -= 15.0
        reasons.append("SELL_VOLUME_ACCELERATING")

    if max_consecutive_red >= 4 and largest_red_atr >= 0.8:
        score -= 20.0
        reasons.append("STRUCTURAL_DAMAGE")
        structural = True
    else:
        structural = False

    score = max(0.0, min(100.0, score))

    return ZoneArrivalFacts(
        score=score,
        arrival_type=arrival,
        arrival_speed_pct=arrival_speed_pct,
        arrival_speed_atr=arrival_speed_atr,
        atr_velocity=atr_velocity,
        bars_to_zone=bars_to_zone,
        red_bar_ratio=round(red_ratio, 2),
        consecutive_red_bars=max_consecutive_red,
        largest_red_bar_atr=round(largest_red_atr, 2) if largest_red_atr else None,
        sell_volume_ratio=round(sell_vol_ratio, 2),
        volume_acceleration=round(vol_accel, 2),
        gap_down_pct=round(gap_down, 2) if gap_down is not None else None,
        crash_velocity=crash,
        structural_damage=structural,
        reason_codes=reasons,
    )
