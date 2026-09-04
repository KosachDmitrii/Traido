"""Desk-facing enrichment for EntryWatch cards — derived UI + likelihood/arrival."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.enums import EntryWatchStatus, SetupType
from core.schemas import Bar, EntryTimingFacts, EntryWatch
from trading.arrival_admission import buy_blocked_for_arrival, evaluate_arrival_gate
from trading.entry_likelihood import evaluate_entry_likelihood
from trading.entry_policy import EntryThresholds, get_entry_thresholds
from trading.entry_watches import price_in_zone, zone_trigger_bounds
from trading.market_context import build_market_context
from trading.zone_arrival import (
    ArrivalType,
    ZoneArrivalFacts,
    evaluate_zone_arrival,
    zone_arrival_required,
)
from trading.zone_touch_calibration import calibration_payload

APPROACHING_ATR = 0.5

# Adaptive entry-watch loop cadence (seconds between passes).
WATCH_POLL_HOT_SEC = 5.0
WATCH_POLL_NEAR_SEC = 10.0
WATCH_POLL_COLD_SEC = 30.0

_HOT_STATUSES = frozenset(
    {
        EntryWatchStatus.TRIGGERED,
        EntryWatchStatus.REVALIDATING,
        EntryWatchStatus.ADMITTED,
        EntryWatchStatus.CONVERTING,
    }
)

# Admission / revalidation codes surfaced on TRIGGER cards (comma bundles included).
_ADMISSION_HINT_CODES = (
    "EXTREME_CHASE",
    "INVALID_STOP",
    "TARGET_UNREALISTIC",
    "EXTREME_SPREAD",
    "SPREAD_TOO_WIDE",
    "SPREAD_ACCEPTABLE",
    "ZONE_ARRIVAL_MISSING",
    "ARRIVAL_CONFIRMATION_MISSING",
    "ZONE_ARRIVAL_QUALITY_LOW",
    "ARRIVAL_TYPE_SELL_OFF",
    "INSUFFICIENT_BARS",
    "SETUP_QUALITY_BELOW_THRESHOLD",
    "ENTRY_QUALITY_BELOW_THRESHOLD",
    "SETUP_BELOW_FLOOR",
    "SETUP_COMPENSATED",
    "ENTRY_BELOW_FLOOR",
    "RR_BELOW_COMPENSATION_FLOOR",
    "WAITING_CONFIRMATION",
    "BUY_ALLOWED",
    "ATR_ONLY_STOP",
    "TARGET_PLAN_MISMATCH",
    "MISSING_TARGET",
    "MISSING_STOP",
    "STALE_DATA",
    "STALE_BARS",
    "MARKET_DATA_UNHEALTHY",
    "DATA_BLOCKED",
    "QUOTE_TIMESTAMP_INVALID",
    "BAR_TIMESTAMP_MISSING",
)


def _hint_from_reason(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("TRIGGERED_CONDITIONS_PENDING:"):
        return text.split(":", 1)[1]
    if text.startswith("INSUFFICIENT_EFFECTIVE_RR:"):
        return text
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if any(
            p in _ADMISSION_HINT_CODES or p.startswith("INSUFFICIENT_EFFECTIVE_RR:") for p in parts
        ):
            return ",".join(parts[:4])
    for code in _ADMISSION_HINT_CODES:
        if text == code or text.startswith(f"{code}:"):
            return text
    return None


# Geometry-only stale hints are no longer hidden in the cushion band — admission uses ask.
_SPREAD_HINT_CODES = frozenset(
    {
        "SPREAD_ACCEPTABLE",
        "SPREAD_TOO_WIDE",
        "EXTREME_SPREAD",
    }
)


def strip_resolved_spread_hints(hint: str | None, *, spread_acceptable: bool) -> str | None:
    """Drop stale spread blocks once live spread passes the policy gate."""
    if not hint or not spread_acceptable:
        return hint
    kept: list[str] = []
    for part in hint.split(","):
        token = part.strip()
        if not token:
            continue
        if token.split(":")[0] not in _SPREAD_HINT_CODES:
            kept.append(token)
    return ",".join(kept[:4]) if kept else None


def desk_revalidation_hint(watch: EntryWatch) -> str | None:
    """Latest admission / trigger reason for desk display (not arrival-only)."""
    if watch.status not in {
        EntryWatchStatus.WAITING,
        EntryWatchStatus.TRIGGERED,
        EntryWatchStatus.REVALIDATING,
        EntryWatchStatus.BLOCKED_DATA,
        EntryWatchStatus.BLOCKED_OPERATIONAL,
    }:
        return None
    px = float(watch.last_price or watch.current_price_at_creation)
    atr_v = watch.admission_snapshot.atr_at_creation if watch.admission_snapshot else None
    if watch.status is EntryWatchStatus.WAITING and not price_in_zone(px, watch, atr=atr_v):
        return None
    hint: str | None = None
    for raw in reversed(watch.reasons or []):
        parsed = _hint_from_reason(raw)
        if parsed:
            hint = parsed
            break
    return hint


def desk_block_reason_from_arrival(arrival: ZoneArrivalFacts, th: EntryThresholds) -> str | None:
    gate = evaluate_arrival_gate(arrival, th)
    return gate.desk_summary()


def _cached_arrival_float(raw: dict[str, object], key: str) -> float | None:
    val = raw.get(key)
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _cached_arrival_int(raw: dict[str, object], key: str) -> int | None:
    val = raw.get(key)
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    return None


def zone_arrival_from_cached(raw: dict[str, object]) -> ZoneArrivalFacts:
    score_raw = raw["score"]
    score = float(score_raw) if isinstance(score_raw, (int, float)) else float(str(score_raw))
    cr = raw.get("consecutive_red_bars") or 0
    consecutive = int(cr) if isinstance(cr, (int, float)) and not isinstance(cr, bool) else 0
    rc = raw.get("reason_codes") or []
    reason_codes = [str(x) for x in rc] if isinstance(rc, list) else []
    return ZoneArrivalFacts(
        score=score,
        arrival_type=ArrivalType(str(raw["arrival_type"])),
        arrival_speed_pct=_cached_arrival_float(raw, "arrival_speed_pct"),
        arrival_speed_atr=_cached_arrival_float(raw, "arrival_speed_atr"),
        atr_velocity=_cached_arrival_float(raw, "atr_velocity"),
        bars_to_zone=_cached_arrival_int(raw, "bars_to_zone"),
        red_bar_ratio=_cached_arrival_float(raw, "red_bar_ratio"),
        consecutive_red_bars=consecutive,
        largest_red_bar_atr=_cached_arrival_float(raw, "largest_red_bar_atr"),
        sell_volume_ratio=_cached_arrival_float(raw, "sell_volume_ratio"),
        volume_acceleration=_cached_arrival_float(raw, "volume_acceleration"),
        gap_down_pct=_cached_arrival_float(raw, "gap_down_pct"),
        crash_velocity=bool(raw.get("crash_velocity")),
        structural_damage=bool(raw.get("structural_damage")),
        reason_codes=reason_codes,
    )


def buy_blocked_from_arrival_dict(payload: dict[str, object], th: EntryThresholds) -> bool:
    """Recompute display block from cached zone_arrival + live thresholds."""
    raw = payload.get("zone_arrival")
    if not isinstance(raw, dict):
        return False
    try:
        arrival = zone_arrival_from_cached(raw)
    except (KeyError, TypeError, ValueError):
        return False
    return buy_blocked_for_arrival(arrival, th)


def watch_poll_hot(watch: EntryWatch) -> bool:
    """True when the watch loop should run a hot cadence for this card."""
    if watch.status in _HOT_STATUSES:
        return True
    enrichment = watch.desk_enrichment or {}
    ui = str(enrichment.get("ui_state") or enrichment.get("status_label") or "")
    if ui in {"IN_ZONE", "TRIGGERED"}:
        return True
    dist = enrichment.get("distance_to_zone_atr")
    if isinstance(dist, (int, float)) and float(dist) <= 0.2:
        return True
    px = float(watch.last_price or watch.current_price_at_creation)
    likelihood = evaluate_entry_likelihood(watch, price=px)
    ui_live = derive_ui_state(watch, price=px, distance_atr=likelihood.distance_atr)
    return ui_live in {"IN_ZONE", "TRIGGERED"}


def watch_poll_near(watch: EntryWatch) -> bool:
    """True when price is approaching the zone (but not yet hot)."""
    if watch_poll_hot(watch):
        return False
    enrichment = watch.desk_enrichment or {}
    ui = str(enrichment.get("ui_state") or enrichment.get("status_label") or "")
    if ui == "APPROACHING":
        return True
    dist = enrichment.get("distance_to_zone_atr")
    if isinstance(dist, (int, float)) and float(dist) <= APPROACHING_ATR:
        return True
    px = float(watch.last_price or watch.current_price_at_creation)
    likelihood = evaluate_entry_likelihood(watch, price=px)
    ui_live = derive_ui_state(watch, price=px, distance_atr=likelihood.distance_atr)
    return ui_live == "APPROACHING"


def watch_loop_interval_sec(watches: list[EntryWatch]) -> float:
    """Adaptive sleep between entry-watch passes."""
    if not watches:
        return WATCH_POLL_COLD_SEC
    if any(watch_poll_hot(w) for w in watches):
        return WATCH_POLL_HOT_SEC
    if any(watch_poll_near(w) for w in watches):
        return WATCH_POLL_NEAR_SEC
    return WATCH_POLL_COLD_SEC


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
    atr_v = facts.atr if facts and facts.atr else None
    if atr_v is None and watch.admission_snapshot and watch.admission_snapshot.atr_at_creation:
        atr_v = watch.admission_snapshot.atr_at_creation
    trig_lo, trig_hi = zone_trigger_bounds(watch, atr=atr_v)
    payload["entry_zone_trigger_low"] = round(trig_lo, 4)
    payload["entry_zone_trigger_high"] = round(trig_hi, 4)

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
        th = get_entry_thresholds()
        payload["buy_blocked"] = buy_blocked_for_arrival(arrival, th)
        payload["desk_block_reason"] = desk_block_reason_from_arrival(arrival, th)
    else:
        payload["zone_arrival"] = None
        payload["zone_arrival_quality"] = None
        payload["zone_arrival_type"] = None
        payload["arrival_reason_codes"] = []
        payload["buy_blocked"] = False
        payload["desk_block_reason"] = None
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

    payload["desk_revalidation_hint"] = desk_revalidation_hint(watch)

    return payload


def zone_arrival_required_for(setup_type: SetupType) -> bool:
    return zone_arrival_required(setup_type)
