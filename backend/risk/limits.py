"""
Risk limits loaded from the locked V1 config.

`configs/v1_paper.json` is the single source of truth for what the desk is
allowed to do. Loading it here means the numbers a reviewer reads in the config
are the numbers the engine actually enforces — no second copy to drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.schemas import RiskLimits

CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "v1_paper.json"

_ALLOWED_KEYS = set(RiskLimits.model_fields.keys())


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or CONFIG_PATH
    if not target.exists():
        return {}
    with target.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def load_risk_limits(path: Path | None = None) -> RiskLimits:
    """
    Build `RiskLimits` from `risk_limits_v1` in the config file.

    Unknown keys are ignored rather than raising: a config that gains a field
    for a future stage must not break the running desk. Missing keys fall back
    to the conservative schema defaults.
    """
    config = load_config(path)
    raw = config.get("risk_limits_v1", {})
    filtered = {k: v for k, v in raw.items() if k in _ALLOWED_KEYS}
    return RiskLimits(**filtered)


@lru_cache
def default_risk_limits() -> RiskLimits:
    """Cached limits for the running process."""
    return load_risk_limits()


@dataclass(frozen=True)
class MacroRegimeThresholds:
    """FRED level gates. Absolute yields, not the tape."""

    dgs10_risk_off: float = 5.5
    dgs10_risk_on: float = 3.0
    unrate_risk_off: float = 5.0


def load_macro_regime(path: Path | None = None) -> MacroRegimeThresholds:
    raw = load_config(path).get("macro_regime", {})
    if not isinstance(raw, dict):
        return MacroRegimeThresholds()
    return MacroRegimeThresholds(
        dgs10_risk_off=float(raw.get("dgs10_risk_off", 5.5)),
        dgs10_risk_on=float(raw.get("dgs10_risk_on", 3.0)),
        unrate_risk_off=float(raw.get("unrate_risk_off", 5.0)),
    )
