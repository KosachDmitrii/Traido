"""Data freshness TTLs — single config for admission and approval paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreshnessPolicy:
    quote_max_age_sec: float = 15.0
    admission_quote_max_age_sec: float = 15.0
    m5_bar_max_age_sec: float = 630.0  # 2 intervals + 30s
    h1_bar_max_age_sec: float = 3900.0  # 1 interval + 5 min
    regime_max_age_sec: float = 300.0  # 5 min for approval-path


FRESHNESS = FreshnessPolicy()
