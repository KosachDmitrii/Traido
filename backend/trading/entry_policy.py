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
    min_buy_quality: int
    """How much of the gap from anchor→price the zone high may cover (0–1)."""
    zone_gap_frac: float
    """VWAP undercut buffer below anchor (ATR multiples). Desk default 0.5."""
    zone_atr_undercut: float
    """Buffer above anchor before chase (ATR multiples). Desk default 0.20."""
    zone_atr_buffer: float
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    a=0 / a=50 match the historical F3→medium curve. a=75 / a=100 soften further
    without the old experimental extremes (VWAP 8%, EMA 18%, 5 ATR).
    """
    a = clamp_aggressiveness(aggressiveness)
    return EntryThresholds(
        aggressiveness=a,
        vwap_ext_pct=_band(a, 1.2, 3.8, 5.0),
        ema_ext_pct=_band(a, 2.8, 7.5, 9.5),
        atr_ext_max=_band(a, 1.6, 2.7, 3.2),
        impulse_atr_max=_band(a, 2.2, 3.2, 3.7),
        drift_high_pct=_band(a, 0.45, 1.1, 1.35),
        min_buy_quality=round(_band(a, 52, 48, 45)),
        zone_gap_frac=_band(a, 0.05, 0.50, 0.65),
        zone_atr_undercut=_band(a, 0.5, 0.40, 0.35),
        zone_atr_buffer=_band(a, 0.22, 0.32, 0.38),
        allow_soft_chase_buy=a >= 25,
        wait_ttl_minutes=round(_band(a, 390, 180, 90)),
        retrace_deep_pct=_band(a, 0.80, 0.86, 0.90),
        retrace_shallow_pct=_band(a, 0.18, 0.11, 0.09),
        retrace_shallow_vwap_pct=_band(a, 0.35, 0.58, 0.68),
        pullback_vol_max=_band(a, 1.05, 1.18, 1.30),
        pullback_index_max=round(_band(a, 3, 4, 5)),
        flag_impulse_weak=a < 25,
        vwap_hold_min_pct=_band(a, -0.40, -0.58, -0.70),
        vwap_anchor_hold_frac=_band(a, 0.995, 0.991, 0.988),
        pullback_vol_digest_max=_band(a, 1.08, 1.18, 1.28),
        resistance_close_pct=_band(a, 0.35, 0.28, 0.22),
        reward_consumed_frac=_band(a, 0.55, 0.62, 0.68),
        atr_extension_min_quality=round(_band(a, 65, 58, 52)),
        chase_wait_quality_buffer=round(_band(a, 12, 8, 6)),
        # Deep pullback → WAIT from medium up; only strong/firmer force NO_TRADE.
        pullback_deep_no_trade=a < 25,
        max_spread_bps=_band(a, 32.0, 38.0, 42.0),
        min_setup_quality=round(_band(a, 55, 50, 48)),
        min_entry_quality=round(_band(a, 50, 46, 44)),
        min_zone_arrival_quality=round(_band(a, 55, 50, 42)),
        allow_fast_pullback=a >= 25,
        # Strong: must already turn up. Weak: allow flat/slightly negative tape.
        momentum_min_pct=_band(a, 0.0, -0.02, -0.15),
        require_momentum_flip=a < 100,
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


def _read_file() -> tuple[int, datetime | None] | tuple[None, None]:
    """Return (aggressiveness, updated_at) from disk, or (None, None)."""
    if not POLICY_PATH.exists():
        return None, None
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("entry policy: unreadable file")
        return None, None
    if isinstance(raw, dict):
        return clamp_aggressiveness(raw.get("aggressiveness", 0)), _parse_ts(raw.get("updated_at"))
    return None, None


def _read_redis() -> tuple[int, datetime | None] | tuple[None, None]:
    client = _redis_client()
    if client is None:
        return None, None
    try:
        raw = client.hget(REDIS_KEY, "aggressiveness")
        ts_raw = client.hget(REDIS_KEY, "updated_at")
    except Exception as exc:  # noqa: BLE001
        logger.warning("entry policy: redis read failed (%s)", type(exc).__name__)
        return None, None
    if raw is None:
        return None, None
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(ts_raw, bytes):
        ts_raw = ts_raw.decode()
    return clamp_aggressiveness(raw), _parse_ts(ts_raw)


def _write_file(aggressiveness: int, *, actor: str, thresholds: EntryThresholds, updated_at: str) -> None:
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
    redis_val, redis_ts = _read_redis()
    file_val, file_ts = _read_file()
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


def get_entry_thresholds() -> EntryThresholds:
    return thresholds_for(get_entry_aggressiveness())


def set_entry_aggressiveness(
    value: float,
    *,
    actor: str = "user",
    experimental: bool = False,
) -> EntryThresholds:
    """Persist (Redis + file) and return the resolved thresholds."""
    global _cached
    a = clamp_aggressiveness(value, experimental=experimental)
    thresholds = thresholds_for(a)
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
