"""Operator entry aggressiveness — how close to market a BUY may fire.

Default 0 preserves the frozen F3 chase floors (pullback near SMA20/VWAP).
Raising it does not touch the risk engine, liquidity, RTH, earnings or news
gates: it only changes whether strategy publishes BUY_NOW vs WAIT_FOR_ENTRY,
and how wide the pullback zone is for watches.

Persisted like the kill switch — a file under data/ — so a restart keeps the
operator's choice.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "entry_policy.json"
_LOCK = threading.Lock()
_cached: int | None = None

# Five operator steps (Сильно → Слабо). UI only offers these; writes snap to them.
ENTRY_LEVELS: tuple[int, ...] = (0, 25, 55, 80, 100)
ENTRY_LEVEL_LABELS: dict[int, str] = {
    0: "strong",
    25: "firmer",
    55: "medium",
    80: "softer",
    100: "weak",
}

# Soft chase: extension / drift. Hard chase still forces WAIT/NO_TRADE even at 100.
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
    """When True, soft chase alone does not block BUY_NOW if quality clears the floor."""
    allow_soft_chase_buy: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp_aggressiveness(value: int | float) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    n = max(0, min(100, n))
    # Snap to the five desk steps so API and UI never disagree.
    return min(ENTRY_LEVELS, key=lambda step: abs(step - n))


def _label(a: int) -> str:
    return ENTRY_LEVEL_LABELS.get(clamp_aggressiveness(a), "strong")


def thresholds_for(aggressiveness: int) -> EntryThresholds:
    """Map 0..100 onto chase floors. 0 = shipped F3 policy."""
    a = clamp_aggressiveness(aggressiveness)
    t = a / 100.0
    return EntryThresholds(
        aggressiveness=a,
        # F3 defaults → room for a stretched name (AAPL-class ~15% above SMA).
        vwap_ext_pct=_lerp(1.0, 8.0, t),
        ema_ext_pct=_lerp(2.5, 18.0, t),
        atr_ext_max=_lerp(1.5, 5.0, t),
        impulse_atr_max=_lerp(2.0, 4.0, t),
        drift_high_pct=_lerp(0.40, 2.0, t),
        min_buy_quality=int(round(_lerp(55, 40, t))),
        zone_gap_frac=_lerp(0.0, 0.85, t),
        allow_soft_chase_buy=a >= 55,
    )


def _read_file() -> int:
    if not POLICY_PATH.exists():
        return 0
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("entry policy: unreadable file, using aggressiveness=0")
        return 0
    if isinstance(raw, dict):
        return clamp_aggressiveness(raw.get("aggressiveness", 0))
    return 0


def get_entry_aggressiveness() -> int:
    global _cached
    with _LOCK:
        if _cached is None:
            _cached = _read_file()
        return _cached


def get_entry_thresholds() -> EntryThresholds:
    return thresholds_for(get_entry_aggressiveness())


def set_entry_aggressiveness(value: int | float, *, actor: str = "user") -> EntryThresholds:
    """Persist and return the resolved thresholds."""
    global _cached
    a = clamp_aggressiveness(value)
    thresholds = thresholds_for(a)
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "aggressiveness": a,
        "actor": actor,
        "thresholds": thresholds.as_dict(),
    }
    POLICY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with _LOCK:
        _cached = a
    logger.info("entry policy: aggressiveness=%s actor=%s", a, actor)
    return thresholds


def reset_entry_policy_cache() -> None:
    """Tests: drop the in-memory cache so the next read hits the file (or 0)."""
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
            "Raises how far above SMA/VWAP a setup may still BUY. "
            "Risk, liquidity, RTH, earnings and news gates are unchanged."
        ),
    }


