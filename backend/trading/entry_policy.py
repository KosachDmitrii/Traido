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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "entry_policy.json"
REDIS_KEY = "traido:entry_policy"
_LOCK = threading.Lock()
_cached: int | None = None

# Five operator steps. Production max is 50; 55+ is experimental paper-only.
ENTRY_LEVELS: tuple[int, ...] = (0, 25, 50)
ENTRY_LEVEL_LABELS: dict[int, str] = {
    0: "strong",
    25: "firmer",
    50: "medium",
}
EXPERIMENTAL_ENTRY_LEVELS: tuple[int, ...] = (55, 80, 100)
EXPERIMENTAL_ENTRY_LEVEL_LABELS: dict[int, str] = {
    55: "medium_experimental",
    80: "softer_experimental",
    100: "weak_experimental",
}
PRODUCTION_MAX_AGGRESSIVENESS = 50
# Soft ceiling for experimental paper-only admin mode (never live).
EXPERIMENTAL_MAX_AGGRESSIVENESS = 100

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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp_aggressiveness(
    value: float,
    *,
    experimental: bool = False,
) -> int:
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return 0
    ceiling = EXPERIMENTAL_MAX_AGGRESSIVENESS if experimental else PRODUCTION_MAX_AGGRESSIVENESS
    n = max(0, min(ceiling, n))
    levels = ENTRY_LEVELS + (EXPERIMENTAL_ENTRY_LEVELS if experimental else ())
    return min(levels, key=lambda step: abs(step - n))


def _label(a: int) -> str:
    a = clamp_aggressiveness(a, experimental=a > PRODUCTION_MAX_AGGRESSIVENESS)
    return ENTRY_LEVEL_LABELS.get(a) or EXPERIMENTAL_ENTRY_LEVEL_LABELS.get(a) or "strong"


def thresholds_for(aggressiveness: int) -> EntryThresholds:
    """Map aggressiveness onto chase floors. Production max 50; higher is experimental."""
    experimental = aggressiveness > PRODUCTION_MAX_AGGRESSIVENESS
    a = clamp_aggressiveness(aggressiveness, experimental=experimental)
    # Normalize against production ceiling so 50 maps to moderate softness,
    # never to the forbidden 100-level floors (VWAP 8%, EMA 18%, 5 ATR, etc.).
    t = min(a, PRODUCTION_MAX_AGGRESSIVENESS) / float(PRODUCTION_MAX_AGGRESSIVENESS)
    return EntryThresholds(
        aggressiveness=a,
        vwap_ext_pct=_lerp(1.0, 3.5, t),
        ema_ext_pct=_lerp(2.5, 7.0, t),
        atr_ext_max=_lerp(1.5, 2.5, t),
        impulse_atr_max=_lerp(2.0, 3.0, t),
        drift_high_pct=_lerp(0.40, 1.0, t),
        min_buy_quality=round(_lerp(55, 50, t)),
        zone_gap_frac=_lerp(0.0, 0.45, t),
        zone_atr_undercut=_lerp(0.5, 0.40, t),
        zone_atr_buffer=_lerp(0.20, 0.30, t),
        allow_soft_chase_buy=a >= 50,
        wait_ttl_minutes=round(_lerp(390, 180, t)),
        retrace_deep_pct=_lerp(0.786, 0.85, t),
        retrace_shallow_pct=_lerp(0.20, 0.12, t),
        retrace_shallow_vwap_pct=_lerp(0.30, 0.55, t),
        pullback_vol_max=_lerp(1.0, 1.15, t),
        pullback_index_max=round(_lerp(3, 4, t)),
        flag_impulse_weak=a < 50,
        vwap_hold_min_pct=_lerp(-0.35, -0.55, t),
        vwap_anchor_hold_frac=_lerp(0.996, 0.992, t),
        pullback_vol_digest_max=_lerp(1.05, 1.15, t),
        resistance_close_pct=_lerp(0.40, 0.30, t),
        reward_consumed_frac=_lerp(0.50, 0.60, t),
        atr_extension_min_quality=round(_lerp(70, 60, t)),
        chase_wait_quality_buffer=round(_lerp(15, 10, t)),
        pullback_deep_no_trade=a < 50,
        max_spread_bps=_lerp(30.0, 35.0, t),
        min_setup_quality=round(_lerp(60, 55, t)),
        min_entry_quality=round(_lerp(55, 50, t)),
        min_zone_arrival_quality=round(_lerp(60, 55, t)),
        allow_fast_pullback=a >= 50,
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


def _read_file() -> int | None:
    """Return aggressiveness from disk, or None when the file is missing."""
    if not POLICY_PATH.exists():
        return None
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("entry policy: unreadable file")
        return None
    if isinstance(raw, dict):
        return clamp_aggressiveness(raw.get("aggressiveness", 0))
    return None


def _read_redis() -> int | None:
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = client.hget(REDIS_KEY, "aggressiveness")
    except Exception as exc:  # noqa: BLE001
        logger.warning("entry policy: redis read failed (%s)", type(exc).__name__)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    return clamp_aggressiveness(raw)


def _write_file(aggressiveness: int, *, actor: str, thresholds: EntryThresholds) -> None:
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "aggressiveness": aggressiveness,
        "actor": actor,
        "thresholds": thresholds.as_dict(),
    }
    POLICY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_redis(aggressiveness: int, *, actor: str) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.hset(
            REDIS_KEY,
            mapping={"aggressiveness": str(aggressiveness), "actor": actor},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("entry policy: redis write failed (%s)", type(exc).__name__)


def _load_aggressiveness() -> int:
    """Redis first (survives redeploy), then file, else strict default."""
    redis_val = _read_redis()
    if redis_val is not None:
        return redis_val
    file_val = _read_file()
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
    _write_file(a, actor=actor, thresholds=thresholds)
    _write_redis(a, actor=actor)
    with _LOCK:
        _cached = a
    logger.info(
        "entry policy: aggressiveness=%s actor=%s experimental=%s",
        a,
        actor,
        experimental,
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
            "Single control for entry timing: chase floors, zone width, WAIT TTL, "
            "pullback/impulse checks, and wait-trigger conditions. "
            "Risk, liquidity, RTH, earnings and news gates are unchanged."
        ),
    }
