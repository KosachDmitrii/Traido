"""Historical zone-touch calibration from shadow outcomes.

No fake probability until sample is sufficient — surfaces LOW/MODERATE/HIGH
with optional calibrated rate when data allows.
"""

from __future__ import annotations

import statistics

from core.enums import SetupType
from core.schemas import ZoneTouchCalibration
from trading.shadow_outcomes import MIN_CALIBRATION_SAMPLES, SHADOW_OUTCOMES, distance_atr_bucket


def lookup_zone_touch_calibration(
    *,
    setup_type: SetupType,
    distance_atr: float | None,
) -> ZoneTouchCalibration:
    bucket = distance_atr_bucket(distance_atr)
    rows = SHADOW_OUTCOMES.list_completed()
    matched = [
        r
        for r in rows
        if r.setup_type == setup_type and distance_atr_bucket(r.distance_atr_at_origin) == bucket
    ]
    if len(matched) < MIN_CALIBRATION_SAMPLES:
        # Fall back to setup_type only when bucket is thin.
        matched = [r for r in rows if r.setup_type == setup_type]
        bucket = "all"

    n = len(matched)
    if n < MIN_CALIBRATION_SAMPLES:
        return ZoneTouchCalibration(
            setup_type=setup_type,
            distance_atr_bucket=bucket,
            zone_touch_rate_pct=0.0,
            sample_size=n,
            calibrated=False,
        )

    touched = sum(1 for r in matched if r.zone_reached)
    rate = touched / n * 100.0
    times = [r.time_to_zone_minutes for r in matched if r.time_to_zone_minutes is not None]
    median_t = int(statistics.median(times)) if times else None

    return ZoneTouchCalibration(
        setup_type=setup_type,
        distance_atr_bucket=bucket,
        zone_touch_rate_pct=round(rate, 1),
        sample_size=n,
        median_time_to_zone_minutes=median_t,
        calibrated=True,
    )


def calibration_payload(
    *,
    setup_type: SetupType,
    distance_atr: float | None,
) -> dict[str, object]:
    cal = lookup_zone_touch_calibration(setup_type=setup_type, distance_atr=distance_atr)
    out = cal.model_dump(mode="json")
    if not cal.calibrated:
        out["display"] = "classification_only"
        out["note"] = (
            f"Need {MIN_CALIBRATION_SAMPLES}+ samples for historical zone-touch rate; "
            f"have {cal.sample_size}."
        )
    else:
        out["display"] = "calibrated_rate"
        out["note"] = (
            f"Historical zone touch rate: {cal.zone_touch_rate_pct:.0f}% "
            f"(sample: {cal.sample_size})"
        )
    return out
