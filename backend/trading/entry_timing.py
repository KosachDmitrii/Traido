"""F3 EntryTimingEngine — deterministic facts for "is this price still a good entry?".

Reuses FeatureSnapshot indicators (VWAP, ATR, EMA, S/R, RVOL). Does not invent
a second technical stack and never calls an LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from core.enums import SessionCohort, Timeframe
from core.schemas import EntryTimingFacts, FeatureSnapshot, MarketAssessment

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
PREMARKET_OPEN = time(4, 0)


def _ind(snap: FeatureSnapshot, key: str) -> float | int | bool | str | None:
    return snap.indicators.get(key)


def session_cohort(ts: datetime | None = None) -> SessionCohort:
    dt = (ts or datetime.now(UTC)).astimezone(ET)
    if dt.weekday() >= 5:
        return SessionCohort.UNKNOWN
    t = dt.time()
    if RTH_OPEN <= t < RTH_CLOSE:
        return SessionCohort.RTH
    if PREMARKET_OPEN <= t < RTH_OPEN:
        return SessionCohort.PREMARKET
    return SessionCohort.AFTER_HOURS


def evaluate_timing(
    exec_snap: FeatureSnapshot,
    *,
    signal_price: float | None = None,
    planned_entry: float | None = None,
    planned_stop: float | None = None,
    planned_target: float | None = None,
    market: MarketAssessment | None = None,
    now: datetime | None = None,
) -> EntryTimingFacts:
    """Compute entry-timing facts from an existing FeatureSnapshot."""
    close = _ind(exec_snap, "close")
    if not isinstance(close, (int, float)) or close <= 0:
        return EntryTimingFacts(
            current_price=0.0,
            session_cohort=session_cohort(now),
        )

    price = float(close)
    atr = _ind(exec_snap, "atr_14")
    atr_f = float(atr) if isinstance(atr, (int, float)) and atr > 0 else None
    vwap = _ind(exec_snap, "vwap")
    ema_fast = _ind(exec_snap, "sma_20")  # live "fast" proxy used by confluence
    ema_slow = _ind(exec_snap, "ema_50")
    rvol = _ind(exec_snap, "relative_volume")
    roc = _ind(exec_snap, "roc_10") or _ind(exec_snap, "momentum_10")

    def _pct_dist(level: float | None) -> float | None:
        if not isinstance(level, (int, float)) or level == 0:
            return None
        return (price - float(level)) / float(level) * 100.0

    dist_vwap = _pct_dist(vwap if isinstance(vwap, (int, float)) else None)
    dist_fast = _pct_dist(ema_fast if isinstance(ema_fast, (int, float)) else None)
    dist_slow = _pct_dist(ema_slow if isinstance(ema_slow, (int, float)) else None)

    atr_extension = None
    if atr_f and isinstance(ema_fast, (int, float)):
        atr_extension = (price - float(ema_fast)) / atr_f

    impulse_ret = float(roc) if isinstance(roc, (int, float)) else None
    impulse_atr = None
    if impulse_ret is not None and atr_f and price > 0:
        impulse_atr = abs(impulse_ret / 100.0 * price) / atr_f

    pullback = None
    if isinstance(ema_fast, (int, float)) and ema_fast > 0 and price <= float(ema_fast):
        pullback = (float(ema_fast) - price) / float(ema_fast) * 100.0
    elif dist_fast is not None and dist_fast > 0:
        pullback = 0.0

    nearest_support = None
    dist_support = None
    supports = [float(s) for s in (exec_snap.support or []) if s < price]
    if supports:
        nearest_support = max(supports)
        dist_support = (price - nearest_support) / price * 100.0

    nearest_resistance = None
    dist_resistance = None
    resistances = [float(r) for r in (exec_snap.resistance or []) if r > price]
    if resistances:
        nearest_resistance = min(resistances)
        dist_resistance = (nearest_resistance - price) / price * 100.0

    drift = None
    if signal_price is not None and signal_price > 0:
        drift = (price - signal_price) / signal_price * 100.0

    remaining_reward = None
    if planned_target is not None and planned_target > price or planned_target is not None:
        remaining_reward = (planned_target - price) / price * 100.0

    # Normal adverse excursion proxy: ~0.5 ATR as a fraction of price.
    normal_retrace = (0.5 * atr_f / price * 100.0) if atr_f else None
    stop_dist_pct = None
    stop_dist_atr = None
    entry_for_stop = planned_entry if planned_entry and planned_entry > 0 else price
    if planned_stop is not None and entry_for_stop > planned_stop:
        stop_dist_pct = (entry_for_stop - planned_stop) / entry_for_stop * 100.0
        if atr_f:
            stop_dist_atr = (entry_for_stop - planned_stop) / atr_f

    # market unused for numeric facts; alignment scored in quality
    del market

    return EntryTimingFacts(
        current_price=price,
        signal_price=signal_price,
        distance_from_vwap_pct=dist_vwap,
        distance_from_fast_ema_pct=dist_fast,
        distance_from_slow_ema_pct=dist_slow,
        atr=atr_f,
        atr_extension=atr_extension,
        recent_impulse_return_pct=impulse_ret,
        recent_impulse_atr=impulse_atr,
        pullback_depth_pct=pullback,
        nearest_support=nearest_support,
        distance_to_support_pct=dist_support,
        nearest_resistance=nearest_resistance,
        distance_to_resistance_pct=dist_resistance,
        relative_volume=float(rvol) if isinstance(rvol, (int, float)) else None,
        short_term_momentum_pct=impulse_ret,
        signal_to_current_drift_pct=drift,
        remaining_expected_reward_pct=remaining_reward,
        normal_expected_retrace_pct=normal_retrace,
        stop_distance_pct=stop_dist_pct,
        stop_distance_atr=stop_dist_atr,
        session_cohort=session_cohort(now),
    )


# ── Chasing / extension reason codes ─────────────────────────────────────────

PRICE_TOO_EXTENDED_FROM_VWAP = "PRICE_TOO_EXTENDED_FROM_VWAP"
PRICE_TOO_EXTENDED_FROM_EMA = "PRICE_TOO_EXTENDED_FROM_EMA"
ATR_EXTENSION_HIGH = "ATR_EXTENSION_HIGH"
IMPULSE_ALREADY_MATURE = "IMPULSE_ALREADY_MATURE"
RESISTANCE_TOO_CLOSE = "RESISTANCE_TOO_CLOSE"
REWARD_ALREADY_CONSUMED = "REWARD_ALREADY_CONSUMED"
ASYMMETRIC_DOWNSIDE = "ASYMMETRIC_DOWNSIDE"
SIGNAL_TO_ENTRY_DRIFT_HIGH = "SIGNAL_TO_ENTRY_DRIFT_HIGH"
NORMAL_RETRACE_EXCEEDS_STOP = "NORMAL_RETRACE_EXCEEDS_STOP"

# Frozen F3 initial policy — aggressiveness 0. Raised via entry_policy.
VWAP_EXT_PCT = 1.0
EMA_EXT_PCT = 2.5
ATR_EXT_MAX = 1.5
IMPULSE_ATR_MAX = 2.0
RESISTANCE_TOO_CLOSE_PCT = 0.40
REWARD_CONSUMED_FRAC = 0.50
DRIFT_HIGH_PCT = 0.40
MIN_BUY_QUALITY = 55


def detect_chasing(
    facts: EntryTimingFacts,
    *,
    thresholds: Any | None = None,
) -> list[str]:
    """Explicit veto codes. High momentum does not override these."""
    from trading.entry_policy import get_entry_thresholds

    th = thresholds if thresholds is not None else get_entry_thresholds()
    vwap_ext = float(getattr(th, "vwap_ext_pct", VWAP_EXT_PCT))
    ema_ext = float(getattr(th, "ema_ext_pct", EMA_EXT_PCT))
    atr_ext = float(getattr(th, "atr_ext_max", ATR_EXT_MAX))
    impulse_max = float(getattr(th, "impulse_atr_max", IMPULSE_ATR_MAX))
    drift_high = float(getattr(th, "drift_high_pct", DRIFT_HIGH_PCT))

    reasons: list[str] = []
    if facts.distance_from_vwap_pct is not None and facts.distance_from_vwap_pct > vwap_ext:
        reasons.append(PRICE_TOO_EXTENDED_FROM_VWAP)
    if facts.distance_from_fast_ema_pct is not None and facts.distance_from_fast_ema_pct > ema_ext:
        reasons.append(PRICE_TOO_EXTENDED_FROM_EMA)
    if facts.atr_extension is not None and facts.atr_extension >= atr_ext:
        reasons.append(ATR_EXTENSION_HIGH)
    if facts.recent_impulse_atr is not None and facts.recent_impulse_atr >= impulse_max:
        reasons.append(IMPULSE_ALREADY_MATURE)
    if (
        facts.distance_to_resistance_pct is not None
        and facts.distance_to_resistance_pct < RESISTANCE_TOO_CLOSE_PCT
    ):
        reasons.append(RESISTANCE_TOO_CLOSE)
    if (
        facts.signal_to_current_drift_pct is not None
        and facts.remaining_expected_reward_pct is not None
        and facts.remaining_expected_reward_pct > 0
        and facts.signal_to_current_drift_pct
        >= REWARD_CONSUMED_FRAC * facts.remaining_expected_reward_pct
    ):
        reasons.append(REWARD_ALREADY_CONSUMED)
    if facts.signal_to_current_drift_pct is not None and facts.signal_to_current_drift_pct > drift_high:
        reasons.append(SIGNAL_TO_ENTRY_DRIFT_HIGH)
    if (
        facts.normal_expected_retrace_pct is not None
        and facts.stop_distance_pct is not None
        and facts.normal_expected_retrace_pct > facts.stop_distance_pct
    ):
        reasons.append(NORMAL_RETRACE_EXCEEDS_STOP)
    if (
        facts.distance_to_resistance_pct is not None
        and facts.stop_distance_pct is not None
        and facts.distance_to_resistance_pct < facts.stop_distance_pct
    ):
        reasons.append(ASYMMETRIC_DOWNSIDE)
    return reasons


def primary_exec_snap(
    features_by_tf: dict[Timeframe, FeatureSnapshot],
) -> FeatureSnapshot:
    return (
        features_by_tf.get(Timeframe.H1)
        or features_by_tf.get(Timeframe.D1)
        or next(iter(features_by_tf.values()))
    )


def zone_from_facts(
    facts: EntryTimingFacts,
    *,
    atr_mult_low: float = 0.3,
    atr_mult_high: float = 0.05,
    thresholds: Any | None = None,
) -> tuple[Decimal, Decimal]:
    """Preferred pullback zone near VWAP/SMA when extended.

    Aggressiveness lifts `zone_gap_frac`: zone high moves toward the live price
    so a WAIT does not demand a full trip back to SMA20.
    """
    from trading.entry_policy import get_entry_thresholds

    th = thresholds if thresholds is not None else get_entry_thresholds()
    gap_frac = float(getattr(th, "zone_gap_frac", 0.0))

    price = facts.current_price
    atr = facts.atr or (price * 0.01)
    anchor = price
    if facts.distance_from_vwap_pct is not None and facts.distance_from_vwap_pct > 0:
        vwap_px = price / (1 + facts.distance_from_vwap_pct / 100.0)
        anchor = vwap_px
    elif facts.distance_from_fast_ema_pct is not None and facts.distance_from_fast_ema_pct > 0:
        ema_px = price / (1 + facts.distance_from_fast_ema_pct / 100.0)
        anchor = ema_px
    low = Decimal(str(round(anchor - atr_mult_low * atr, 4)))
    toward_price = max(0.0, price - anchor) * gap_frac
    high = Decimal(
        str(round(min(price, anchor + atr_mult_high * atr + toward_price), 4))
    )
    if low >= high:
        high = Decimal(str(round(float(low) + max(atr * 0.1, price * 0.001), 4)))
    return low, high
