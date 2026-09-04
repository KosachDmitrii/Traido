"""Operator entry aggressiveness — single desk control for entry timing.

Default 0 preserves the frozen F3 chase floors (pullback near SMA20/VWAP).
Raising it loosens chase, widens/nudges the zone toward price, lengthens or
shortens WAIT TTL, and relaxes wait-trigger checks. It does not touch the risk
engine, liquidity, RTH, earnings or news gates.

Persisted like the kill switch: Redis when configured (survives Railway
redeploys), plus a file under data/ so a Redis outage still keeps the last
choice on a single node.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "entry_policy.json"
REDIS_KEY = "traido:entry_policy"
_LOCK = threading.Lock()
_cached: int | None = None

# Five production desk steps — Сильно → Слабо. Must match frontend ENTRY_STEPS.
ENTRY_LEVELS: tuple[int, ...] = (0, 25, 50, 75, 100)
ENTRY_LEVEL_LABELS: dict[int, str] = {
    0: "strong",
    25: "firmer",
    50: "medium",
    75: "softer",
    100: "weak",
}
# Legacy aliases: experimental used to gate 55/80/100; those are production now.
EXPERIMENTAL_ENTRY_LEVELS: tuple[int, ...] = ()
EXPERIMENTAL_ENTRY_LEVEL_LABELS: dict[int, str] = {}
PRODUCTION_MAX_AGGRESSIVENESS = 100
EXPERIMENTAL_MAX_AGGRESSIVENESS = 100
# Historical production soft end (old max=50). Levels above this extend further.
_MEDIUM_AGGRESSIVENESS = 50

# Soft chase: extension / drift. Hard chase still forces WAIT/NO_TRADE even at max.
SOFT_CHASE_CODES = frozenset(
    {
        "PRICE_TOO_EXTENDED_FROM_VWAP",
        "PRICE_TOO_EXTENDED_FROM_EMA",
        "ATR_EXTENSION_HIGH",
        "IMPULSE_ALREADY_MATURE",
        "SIGNAL_TO_ENTRY_DRIFT_HIGH",
    }
)


@dataclass(frozen=True)
class EntryThresholds:
    """Resolved numbers decide_entry / detect_chasing / zone_from_facts consume."""

    aggressiveness: int
    vwap_ext_pct: float
    ema_ext_pct: float
    atr_ext_max: float
    impulse_atr_max: float
    drift_high_pct: float
    """How much of the gap from anchor→price the zone high may cover (0–1)."""
    zone_gap_frac: float
    """VWAP undercut buffer below anchor (ATR multiples). Desk default 0.5."""
    zone_atr_undercut: float
    """Buffer above anchor before chase (ATR multiples). Desk default 0.20."""
    zone_atr_buffer: float
    """Hard cap on zone width in ATR — stops 8-ATR 'canyons' that never get touched."""
    zone_max_width_atr: float
    """When True, soft chase alone does not block BUY_NOW if quality clears the floor."""
    allow_soft_chase_buy: bool
    """How long a WAIT plan stays actionable before WAIT_EXPIRED."""
    wait_ttl_minutes: int
    """Fib / leg retracement above which PULLBACK_TOO_DEEP fires (0–1)."""
    retrace_deep_pct: float
    """Retracement below which PULLBACK_TOO_SHALLOW may fire (0–1)."""
    retrace_shallow_pct: float
    """Min distance from VWAP (%) paired with shallow retracement check."""
    retrace_shallow_vwap_pct: float
    """Pullback volume ratio above which PULLBACK_VOL_HEAVY fires."""
    pullback_vol_max: float
    """Consecutive pullback bars before PULLBACK_EXHAUSTED."""
    pullback_index_max: int
    """Grade-C impulse flagged as IMPULSE_WEAK when True."""
    flag_impulse_weak: bool
    """VWAP hold: fail when distance_from_vwap_pct falls below this."""
    vwap_hold_min_pct: float
    """VWAP hold: fail when price breaks below anchor * this fraction."""
    vwap_anchor_hold_frac: float
    """WAIT trigger: max pullback_vol_ratio before PULLBACK_VOL_DIGESTING fails."""
    pullback_vol_digest_max: float
    """RESISTANCE_TOO_CLOSE when distance_to_resistance_pct below this."""
    resistance_close_pct: float
    """REWARD_ALREADY_CONSUMED drift fraction of remaining reward."""
    reward_consumed_frac: float
    """decide_entry: ATR_EXTENSION_HIGH forces WAIT below this quality."""
    atr_extension_min_quality: int
    """Extra quality buffer when chase codes present before WAIT."""
    chase_wait_quality_buffer: int
    """When True, PULLBACK_TOO_DEEP is NO_TRADE; else WAIT only."""
    pullback_deep_no_trade: bool
    """Max spread (bps) on WAIT revalidation."""
    max_spread_bps: float
    """TradeAdmission soft floors — aggressiveness may lower these, not hard vetoes."""
    min_setup_quality: int
    min_entry_quality: int
    """Zone arrival gate for pullback-style setups (Phase 2.8)."""
    min_zone_arrival_quality: int
    """When False, FAST_PULLBACK below min_zone_arrival blocks BUY."""
    allow_fast_pullback: bool
    """WAIT revalidation: short-term momentum must be above this pct (0 = must flip +)."""
    momentum_min_pct: float
    """When False, MOMENTUM_TURNS_POSITIVE is not required to leave WAIT."""
    require_momentum_flip: bool
    """Minimum effective R:R after spread/slippage (TradeAdmission)."""
    min_effective_rr: float
    """When setup_quality < 55, admission R:R floor (steps down with aggressiveness)."""
    weak_setup_min_rr: float
    """When False, VWAP_HOLDS is not required after TRIGGERED."""
    require_vwap_hold: bool
    """When False, PULLBACK_VOL_DIGESTING is not required after TRIGGERED."""
    require_vol_digest: bool
    """When True, SELL_OFF arrivals may pass at min_sell_off_arrival_quality."""
    allow_sell_off_arrival: bool
    """Minimum arrival score for SELL_OFF when allow_sell_off_arrival."""
    min_sell_off_arrival_quality: int
    """Minimum arrival score for FAST_PULLBACK (replaces hardcoded 45 floor)."""
    min_fast_pullback_arrival_quality: int
    """When True, zone_arrival.structural_damage is a hard veto."""
    structural_arrival_hard: bool
    """Max bid/ask age (seconds) for admission revalidation at this level."""
    quote_max_age_sec: float
    """Minimum zone width as ATR multiple (Keltner-style floor)."""
    zone_min_width_atr: float
    """Minimum zone width as fraction of price (~0.15%)."""
    zone_min_width_pct: float
    """Fib impulse band buffer as fraction of impulse range."""
    fib_buffer_frac: float
    """When True, TRIGGERED requires price in upper half of zone."""
    zone_require_reclaim: bool
    """Invalidate after this many zone touches without conversion."""
    zone_max_touch_count: int
    """Invalidate when price falls this many ATR below zone_low."""
    zone_invalidate_below_atr: float
    """Stop placement: ATR below zone/swing for wait plans."""
    zone_stop_atr: float
    # ── Trader agent structure/setup floors (same five desk steps) ───────────
    require_uptrend: bool
    allow_range: bool
    require_ema_stack: bool
    rsi_overbought: float
    chase_ext_frac: float
    near_sma_frac: float
    allow_below_sma: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Explicit five-rung desk steps — operator-visible knobs, not interpolated surprises.
_STEP_MIN_EFFECTIVE_RR: dict[int, float] = {
    0: 2.0,
    25: 1.9,
    50: 1.75,
    75: 1.6,
    100: 1.45,
}
# Weak-setup penalty — at «Слабо» matches min_effective_rr so the desk knob is honest.
_STEP_WEAK_SETUP_MIN_RR: dict[int, float] = {
    0: 2.5,
    25: 2.3,
    50: 2.1,
    75: 1.8,
    100: 1.45,
}
_STEP_MIN_ZONE_ARRIVAL: dict[int, int] = {
    0: 60,
    25: 56,
    50: 50,
    75: 44,
    100: 35,
}
_STEP_REQUIRE_VWAP_HOLD: dict[int, bool] = {
    0: True,
    25: True,
    50: True,
    75: False,
    100: False,
}
_STEP_REQUIRE_VOL_DIGEST: dict[int, bool] = {
    0: True,
    25: True,
    50: True,
    75: False,
    100: False,
}
_STEP_ALLOW_SELL_OFF: dict[int, bool] = {
    0: False,
    25: False,
    50: False,
    75: True,
    100: True,
}
_STEP_MIN_SELL_OFF_ARRIVAL: dict[int, int] = {
    0: 60,
    25: 56,
    50: 50,
    75: 22,
    100: 8,
}
_STEP_MIN_FAST_PULLBACK_ARRIVAL: dict[int, int] = {
    0: 58,
    25: 52,
    50: 48,
    75: 40,
    100: 28,
}
_STEP_STRUCTURAL_ARRIVAL_HARD: dict[int, bool] = {
    0: True,
    25: True,
    50: True,
    75: False,
    100: False,
}
_STEP_QUOTE_MAX_AGE_SEC: dict[int, float] = {
    0: 15.0,
    25: 20.0,
    50: 30.0,
    75: 45.0,
    100: 90.0,
}
_STEP_ZONE_K_UNDER: dict[int, float] = {
    0: 0.75,
    25: 0.70,
    50: 0.65,
    75: 0.60,
    100: 0.55,
}
_STEP_ZONE_K_OVER: dict[int, float] = {
    0: 0.25,
    25: 0.30,
    50: 0.35,
    75: 0.40,
    100: 0.45,
}
_STEP_ZONE_MIN_WIDTH_ATR: dict[int, float] = {
    0: 0.50,
    25: 0.45,
    50: 0.40,
    75: 0.35,
    100: 0.30,
}
_STEP_ZONE_MIN_WIDTH_PCT: dict[int, float] = {
    0: 0.0015,
    25: 0.0015,
    50: 0.0015,
    75: 0.0015,
    100: 0.0015,
}
_STEP_FIB_BUFFER: dict[int, float] = {
    0: 0.02,
    25: 0.025,
    50: 0.03,
    75: 0.035,
    100: 0.04,
}
_STEP_ZONE_REQUIRE_RECLAIM: dict[int, bool] = {
    0: True,
    25: True,
    50: True,
    75: False,
    100: False,
}
_STEP_ZONE_MAX_TOUCHES: dict[int, int] = {
    0: 2,
    25: 2,
    50: 3,
    75: 4,
    100: 5,
}
_STEP_ZONE_INVALIDATE_BELOW_ATR: dict[int, float] = {
    0: 0.35,
    25: 0.40,
    50: 0.45,
    75: 0.50,
    100: 0.55,
}
_STEP_ZONE_STOP_ATR: dict[int, float] = {
    0: 2.0,
    25: 1.9,
    50: 1.75,
    75: 1.6,
    100: 1.5,
}
_STEP_MIN_SETUP_QUALITY: dict[int, int] = {
    0: 60,
    25: 58,
    50: 55,
    75: 51,
    100: 48,
}
_STEP_MIN_ENTRY_QUALITY: dict[int, int] = {
    0: 55,
    25: 53,
    50: 50,
    75: 47,
    100: 44,
}
_STEP_REQUIRE_UPTREND: dict[int, bool] = {
    0: True,
    25: True,
    50: False,
    75: False,
    100: False,
}
_STEP_ALLOW_RANGE: dict[int, bool] = {
    0: False,
    25: False,
    50: True,
    75: True,
    100: True,
}
_STEP_REQUIRE_EMA_STACK: dict[int, bool] = {
    0: True,
    25: True,
    50: True,
    75: False,
    100: False,
}
_STEP_RSI_OVERBOUGHT: dict[int, float] = {
    0: 70.0,
    25: 72.0,
    50: 74.0,
    75: 77.0,
    100: 80.0,
}
_STEP_CHASE_EXT_FRAC: dict[int, float] = {
    0: 0.035,
    25: 0.040,
    50: 0.048,
    75: 0.055,
    100: 0.070,
}
_STEP_NEAR_SMA_FRAC: dict[int, float] = {
    0: 0.025,
    25: 0.028,
    50: 0.032,
    75: 0.038,
    100: 0.045,
}
_STEP_ALLOW_BELOW_SMA: dict[int, bool] = {
    0: False,
    25: True,
    50: True,
    75: True,
    100: True,
}


def _step_value(table: dict[int, float | int | bool], aggressiveness: int) -> float | int | bool:
    a = clamp_aggressiveness(aggressiveness)
    return table[a]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp_aggressiveness(
    value: float,
    *,
    experimental: bool = False,
) -> int:
    """Snap to a production desk step. `experimental` kept for call-site compat."""
    _ = experimental
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return 0
    n = max(0, min(PRODUCTION_MAX_AGGRESSIVENESS, n))
    return min(ENTRY_LEVELS, key=lambda step: abs(step - n))


def _label(a: int) -> str:
    a = clamp_aggressiveness(a)
    return ENTRY_LEVEL_LABELS.get(a) or "strong"


def _band(a: int, v0: float, v50: float, v100: float) -> float:
    """Keep a=0 and a=50 identical to the old three-step curve; extend 50→100."""
    if a <= _MEDIUM_AGGRESSIVENESS:
        return _lerp(v0, v50, a / float(_MEDIUM_AGGRESSIVENESS))
    return _lerp(
        v50,
        v100,
        (a - _MEDIUM_AGGRESSIVENESS)
        / float(PRODUCTION_MAX_AGGRESSIVENESS - _MEDIUM_AGGRESSIVENESS),
    )


def thresholds_for(aggressiveness: int) -> EntryThresholds:
    """Map aggressiveness onto chase floors across the five production steps.

    a=0 matches main/historical F3 strong floors — do not soften the default.
    a=50 is the medium desk step; a=75 / a=100 soften further (Weak zone
    geometry is touchable) without the old experimental extremes.
    """
    a = clamp_aggressiveness(aggressiveness)
    return EntryThresholds(
        aggressiveness=a,
        vwap_ext_pct=_band(a, 1.0, 3.5, 5.0),
        ema_ext_pct=_band(a, 2.5, 7.0, 9.5),
        atr_ext_max=_band(a, 1.5, 2.5, 3.2),
        impulse_atr_max=_band(a, 2.0, 3.0, 3.7),
        drift_high_pct=_band(a, 0.40, 1.0, 1.35),
        zone_gap_frac=_band(a, 0.0, 0.45, 0.82),
        zone_atr_undercut=float(_step_value(_STEP_ZONE_K_UNDER, a)),
        zone_atr_buffer=float(_step_value(_STEP_ZONE_K_OVER, a)),
        zone_max_width_atr=_band(a, 3.5, 3.0, 2.5),
        allow_soft_chase_buy=a >= _MEDIUM_AGGRESSIVENESS,
        wait_ttl_minutes=round(_band(a, 390, 180, 150)),
        retrace_deep_pct=_band(a, 0.786, 0.85, 0.90),
        retrace_shallow_pct=_band(a, 0.20, 0.12, 0.09),
        retrace_shallow_vwap_pct=_band(a, 0.30, 0.55, 0.68),
        pullback_vol_max=_band(a, 1.0, 1.15, 1.30),
        pullback_index_max=round(_band(a, 3, 4, 5)),
        flag_impulse_weak=a < _MEDIUM_AGGRESSIVENESS,
        vwap_hold_min_pct=_band(a, -0.35, -0.55, -0.70),
        vwap_anchor_hold_frac=_band(a, 0.996, 0.992, 0.988),
        pullback_vol_digest_max=_band(a, 1.05, 1.15, 1.28),
        resistance_close_pct=_band(a, 0.40, 0.30, 0.22),
        reward_consumed_frac=_band(a, 0.50, 0.60, 0.68),
        atr_extension_min_quality=round(_band(a, 70, 60, 52)),
        chase_wait_quality_buffer=round(_band(a, 15, 10, 6)),
        pullback_deep_no_trade=a < _MEDIUM_AGGRESSIVENESS,
        max_spread_bps=_band(a, 30.0, 35.0, 42.0),
        min_setup_quality=int(_step_value(_STEP_MIN_SETUP_QUALITY, a)),
        min_entry_quality=int(_step_value(_STEP_MIN_ENTRY_QUALITY, a)),
        min_zone_arrival_quality=int(_step_value(_STEP_MIN_ZONE_ARRIVAL, a)),
        allow_fast_pullback=a >= _MEDIUM_AGGRESSIVENESS,
        # Strong: must already turn up. Weak: allow flat/slightly negative tape.
        momentum_min_pct=_band(a, 0.0, -0.02, -0.15),
        require_momentum_flip=a < 100,
        min_effective_rr=float(_step_value(_STEP_MIN_EFFECTIVE_RR, a)),
        weak_setup_min_rr=float(_step_value(_STEP_WEAK_SETUP_MIN_RR, a)),
        require_vwap_hold=bool(_step_value(_STEP_REQUIRE_VWAP_HOLD, a)),
        require_vol_digest=bool(_step_value(_STEP_REQUIRE_VOL_DIGEST, a)),
        allow_sell_off_arrival=bool(_step_value(_STEP_ALLOW_SELL_OFF, a)),
        min_sell_off_arrival_quality=int(_step_value(_STEP_MIN_SELL_OFF_ARRIVAL, a)),
        min_fast_pullback_arrival_quality=int(_step_value(_STEP_MIN_FAST_PULLBACK_ARRIVAL, a)),
        structural_arrival_hard=bool(_step_value(_STEP_STRUCTURAL_ARRIVAL_HARD, a)),
        quote_max_age_sec=float(_step_value(_STEP_QUOTE_MAX_AGE_SEC, a)),
        zone_min_width_atr=float(_step_value(_STEP_ZONE_MIN_WIDTH_ATR, a)),
        zone_min_width_pct=float(_step_value(_STEP_ZONE_MIN_WIDTH_PCT, a)),
        fib_buffer_frac=float(_step_value(_STEP_FIB_BUFFER, a)),
        zone_require_reclaim=bool(_step_value(_STEP_ZONE_REQUIRE_RECLAIM, a)),
        zone_max_touch_count=int(_step_value(_STEP_ZONE_MAX_TOUCHES, a)),
        zone_invalidate_below_atr=float(_step_value(_STEP_ZONE_INVALIDATE_BELOW_ATR, a)),
        zone_stop_atr=float(_step_value(_STEP_ZONE_STOP_ATR, a)),
        require_uptrend=bool(_step_value(_STEP_REQUIRE_UPTREND, a)),
        allow_range=bool(_step_value(_STEP_ALLOW_RANGE, a)),
        require_ema_stack=bool(_step_value(_STEP_REQUIRE_EMA_STACK, a)),
        rsi_overbought=float(_step_value(_STEP_RSI_OVERBOUGHT, a)),
        chase_ext_frac=float(_step_value(_STEP_CHASE_EXT_FRAC, a)),
        near_sma_frac=float(_step_value(_STEP_NEAR_SMA_FRAC, a)),
        allow_below_sma=bool(_step_value(_STEP_ALLOW_BELOW_SMA, a)),
    )


def _redis_client() -> Any:
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        logger.warning("entry policy: redis unavailable (%s)", type(exc).__name__)
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _read_file() -> tuple[int | None, datetime | None, str | None]:
    """Return (aggressiveness, updated_at, actor) from disk."""
    if not POLICY_PATH.exists():
        return None, None, None
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("entry policy: unreadable file")
        return None, None, None
    if isinstance(raw, dict):
        actor = raw.get("actor")
        return (
            clamp_aggressiveness(raw.get("aggressiveness", 0)),
            _parse_ts(raw.get("updated_at")),
            actor if isinstance(actor, str) else None,
        )
    return None, None, None


def _read_redis() -> tuple[int | None, datetime | None, str | None]:
    client = _redis_client()
    if client is None:
        return None, None, None
    try:
        raw = client.hget(REDIS_KEY, "aggressiveness")
        ts_raw = client.hget(REDIS_KEY, "updated_at")
        actor_raw = client.hget(REDIS_KEY, "actor")
    except Exception as exc:  # noqa: BLE001
        logger.warning("entry policy: redis read failed (%s)", type(exc).__name__)
        return None, None, None
    if raw is None:
        return None, None, None
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(ts_raw, bytes):
        ts_raw = ts_raw.decode()
    if isinstance(actor_raw, bytes):
        actor_raw = actor_raw.decode()
    actor = actor_raw if isinstance(actor_raw, str) and actor_raw else None
    return clamp_aggressiveness(raw), _parse_ts(ts_raw), actor


def _heal_redis_from_file(
    aggressiveness: int,
    updated_at: datetime | None,
    *,
    actor: str | None,
) -> None:
    """Push the on-disk operator choice back into Redis after test pollution."""
    ts = updated_at or datetime.now(UTC)
    _write_redis(aggressiveness, actor=actor or "user", updated_at=ts.isoformat())


def _write_file(
    aggressiveness: int, *, actor: str, thresholds: EntryThresholds, updated_at: str
) -> None:
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "aggressiveness": aggressiveness,
        "actor": actor,
        "updated_at": updated_at,
        "thresholds": thresholds.as_dict(),
    }
    POLICY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_redis(aggressiveness: int, *, actor: str, updated_at: str) -> bool:
    client = _redis_client()
    if client is None:
        return False
    try:
        client.hset(
            REDIS_KEY,
            mapping={
                "aggressiveness": str(aggressiveness),
                "actor": actor,
                "updated_at": updated_at,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("entry policy: redis write failed (%s)", type(exc).__name__)
        return False


def _load_aggressiveness() -> int:
    """Prefer the newer of Redis vs file when both exist (stale Redis used to win)."""
    redis_val, redis_ts, redis_actor = _read_redis()
    file_val, file_ts, file_actor = _read_file()

    # Unit tests persist actor=test into shared Redis; never let that pin the desk.
    if redis_actor == "test" and file_val is not None:
        logger.warning(
            "entry policy: ignoring redis test=%s; using file=%s (actor=%s)",
            redis_val,
            file_val,
            file_actor,
        )
        _heal_redis_from_file(file_val, file_ts, actor=file_actor)
        return file_val

    if redis_val is not None and file_val is not None:
        if file_ts is not None and redis_ts is not None:
            return file_val if file_ts >= redis_ts else redis_val
        if file_ts is not None and redis_ts is None:
            return file_val
        if redis_ts is not None and file_ts is None:
            return redis_val
        # No timestamps: file is the local source of truth when Redis may be stale.
        if redis_val != file_val:
            logger.warning(
                "entry policy: redis=%s file=%s disagree without updated_at; preferring file",
                redis_val,
                file_val,
            )
            return file_val
        return redis_val
    if redis_val is not None:
        return redis_val
    if file_val is not None:
        return file_val
    return 0


def get_entry_aggressiveness() -> int:
    global _cached
    with _LOCK:
        if _cached is None:
            _cached = _load_aggressiveness()
        return _cached


def _with_feed_spread(th: EntryThresholds) -> EntryThresholds:
    """Widen spread cap on IEX — single-exchange quotes run wider than SIP/NBBO."""
    from dataclasses import replace

    from core.config import get_settings
    from market_data.factory import resolve_alpaca_data_feed
    from market_data.spread_threshold import max_spread_bps_for_feed

    feed = resolve_alpaca_data_feed(get_settings())
    adjusted = max_spread_bps_for_feed(th.max_spread_bps, feed)
    if adjusted == th.max_spread_bps:
        return th
    return replace(th, max_spread_bps=adjusted)


def get_entry_thresholds() -> EntryThresholds:
    return _with_feed_spread(thresholds_for(get_entry_aggressiveness()))


def set_entry_aggressiveness(
    value: float,
    *,
    actor: str = "user",
    experimental: bool = False,
) -> EntryThresholds:
    """Persist (Redis + file) and return the resolved thresholds."""
    global _cached
    a = clamp_aggressiveness(value, experimental=experimental)
    thresholds = _with_feed_spread(thresholds_for(a))
    updated_at = datetime.now(UTC).isoformat()
    _write_file(a, actor=actor, thresholds=thresholds, updated_at=updated_at)
    wrote_redis = _write_redis(a, actor=actor, updated_at=updated_at)
    with _LOCK:
        _cached = a
    logger.info(
        "entry policy: aggressiveness=%s actor=%s experimental=%s redis=%s",
        a,
        actor,
        experimental,
        "ok" if wrote_redis else "skip",
    )
    return thresholds


def reset_entry_policy_cache() -> None:
    """Tests: drop the in-memory cache so the next read hits Redis/file (or 0)."""
    global _cached
    with _LOCK:
        _cached = None


def policy_payload() -> dict[str, Any]:
    th = get_entry_thresholds()
    return {
        "aggressiveness": th.aggressiveness,
        "label": _label(th.aggressiveness),
        "thresholds": th.as_dict(),
        "soft_chase_codes": sorted(SOFT_CHASE_CODES),
        "note": (
            "Single control for entry timing and trader-desk Structure/Setup "
            "floors (HTF trend, RSI, chase distance). "
            "Risk, liquidity, RTH, earnings and news gates are unchanged."
        ),
    }
