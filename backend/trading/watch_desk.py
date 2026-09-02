"""Desk-facing enrichment for EntryWatch cards — derived UI + likelihood/arrival."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.enums import EntryWatchStatus, SetupType
from core.schemas import Bar, EntryTimingFacts, EntryWatch
from trading.entry_likelihood import evaluate_entry_likelihood
from trading.entry_watches import price_in_zone
from trading.market_context import build_market_context
from trading.zone_arrival import evaluate_zone_arrival, zone_arrival_required
from trading.zone_touch_calibration import calibration_payload

APPROACHING_ATR = 0.5


def derive_ui_state(watch: EntryWatch, *, price: float, distance_atr: float | None) -> str:
    in_zone = price_in_zone(price, watch)
    if watch.status is EntryWatchStatus.TRIGGERED and in_zone:
        return "TRIGGERED"
    if in_zone:
        return "IN_ZONE"
    if distance_atr is not None and distance_atr <= APPROACHING_ATR:
        return "APPROACHING"
    return "WAITING"


def enrich_watch_for_desk(
    watch: EntryWatch,
    *,
    price: float | None = None,
    facts: EntryTimingFacts | None = None,
    bars: list[Bar] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge watch JSON with live likelihood, arrival, and derived UI state.

    Returns a flat display dict — never nests a previous desk_enrichment.
    """
    now = now or datetime.now(UTC)
    px = price
    if px is None and watch.last_price is not None:
        px = float(watch.last_price)
    if px is None:
        px = float(watch.current_price_at_creation)

    likelihood = evaluate_entry_likelihood(watch, price=px, facts=facts, now=now)
    ui_state = derive_ui_state(watch, price=px, distance_atr=likelihood.distance_atr)

    # Start from machine fields but strip nested enrichment to avoid recursive growth.
    payload = watch.model_dump(mode="json")
    payload.pop("desk_enrichment", None)
    payload["ui_state"] = ui_state
    payload["entry_likelihood"] = likelihood.as_dict()
    payload["distance_to_zone_pct"] = likelihood.distance_pct
    payload["distance_to_zone_atr"] = likelihood.distance_atr
    payload["setup_quality"] = watch.setup_quality_at_creation or (
        watch.candidate.setup_quality if watch.candidate else None
    )
    payload["entry_quality"] = watch.entry_quality_at_creation
    payload["setup_type"] = watch.setup_type.value
    payload["market_context"] = build_market_context(symbol=watch.symbol).as_dict()

    in_zone = ui_state in {"IN_ZONE", "TRIGGERED"}
    if in_zone and bars:
        atr = facts.atr if facts else None
        if watch.admission_snapshot and watch.admission_snapshot.atr_at_creation:
            atr = atr or watch.admission_snapshot.atr_at_creation
        arrival = evaluate_zone_arrival(watch, bars, atr=atr, current_price=px)
        payload["zone_arrival"] = arrival.as_dict()
        payload["zone_arrival_quality"] = round(arrival.score)
        payload["zone_arrival_type"] = arrival.arrival_type.value
        payload["arrival_reason_codes"] = arrival.reason_codes
        if arrival.crash_velocity or arrival.structural_damage:
            payload["buy_blocked"] = True
        else:
            from trading.entry_policy import get_entry_thresholds

            min_arrival = get_entry_thresholds().min_zone_arrival_quality
            payload["buy_blocked"] = arrival.score < min_arrival
    else:
        payload["zone_arrival"] = None
        payload["zone_arrival_quality"] = None
        payload["zone_arrival_type"] = None
        payload["arrival_reason_codes"] = []
        payload["buy_blocked"] = False
        try:
            payload["zone_touch_calibration"] = calibration_payload(
                setup_type=watch.setup_type or SetupType.PULLBACK_CONTINUATION,
                distance_atr=likelihood.distance_atr,
            )
        except Exception:  # noqa: BLE001 — display-only; never fail the desk card
            payload["zone_touch_calibration"] = None

    if ui_state == "APPROACHING":
        payload["status_label"] = "APPROACHING"
    elif ui_state == "IN_ZONE":
        payload["status_label"] = "IN_ZONE"
    elif ui_state == "TRIGGERED":
        payload["status_label"] = "TRIGGERED"
    else:
        payload["status_label"] = "WAITING"

    return payload


def zone_arrival_required_for(setup_type: SetupType) -> bool:
    return zone_arrival_required(setup_type)
