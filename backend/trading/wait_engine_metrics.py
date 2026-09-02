"""WAIT engine quality metrics from shadow outcomes."""

from __future__ import annotations

import statistics

from core.schemas import StrictModel
from trading.shadow_outcomes import SHADOW_OUTCOMES


class WaitEngineMetrics(StrictModel):
    sample_size: int
    pct_wait_reached_zone: float | None = None
    median_time_to_zone_minutes: int | None = None
    pct_zone_became_buy: float | None = None
    pct_expired_later_reached_zone: float | None = None
    falling_knife_avoidance_pct: float | None = None


def compute_wait_engine_metrics(*, limit: int = 2000) -> WaitEngineMetrics:
    rows = SHADOW_OUTCOMES.list_completed(limit=limit)
    n = len(rows)
    if n == 0:
        return WaitEngineMetrics(sample_size=0)

    reached = [r for r in rows if r.zone_reached]
    expired = [r for r in rows if r.origin == "watch_terminal" and not r.zone_reached]
    expired_reached = [r for r in rows if r.origin == "watch_terminal" and r.zone_reached]

    times = [r.time_to_zone_minutes for r in reached if r.time_to_zone_minutes is not None]
    median_t = int(statistics.median(times)) if times else None

    # Falling knife: admission blocked / low arrival, price kept falling (mae < -3%)
    knives = [
        r
        for r in rows
        if r.zone_reached
        and r.zone_arrival_quality is not None
        and r.zone_arrival_quality < 60
    ]
    avoided = [r for r in knives if r.mae_pct is not None and r.mae_pct <= -3.0]
    fk_rate = len(avoided) / len(knives) * 100.0 if knives else None

    return WaitEngineMetrics(
        sample_size=n,
        pct_wait_reached_zone=round(len(reached) / n * 100.0, 1) if n else None,
        median_time_to_zone_minutes=median_t,
        pct_expired_later_reached_zone=(
            round(len(expired_reached) / max(len(expired) + len(expired_reached), 1) * 100.0, 1)
            if expired or expired_reached
            else None
        ),
        falling_knife_avoidance_pct=round(fk_rate, 1) if fk_rate is not None else None,
    )
