"""Apply desk enrichment during watch loop passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.enums import EntryWatchStatus, Timeframe
from core.schemas import Bar, EntryTimingFacts, EntryWatch, Quote
from quant.engine import compute_features
from trading.entry_policy import get_entry_thresholds
from trading.entry_timing import evaluate_timing
from trading.entry_watches import price_in_zone, zone_trigger_bounds
from trading.watch_desk import (
    buy_blocked_from_arrival_dict,
    derive_ui_state,
    desk_block_reason_from_arrival,
    desk_revalidation_hint,
    enrich_watch_for_desk,
    strip_resolved_spread_hints,
)

# Display-only keys cached on the watch. Machine state always comes from the row.
_DISPLAY_KEYS = frozenset(
    {
        "ui_state",
        "entry_likelihood",
        "distance_to_zone_pct",
        "distance_to_zone_atr",
        "setup_quality",
        "entry_quality",
        "setup_type",
        "market_context",
        "zone_arrival",
        "zone_arrival_quality",
        "zone_arrival_type",
        "arrival_reason_codes",
        "buy_blocked",
        "desk_block_reason",
        "zone_touch_calibration",
        "enriched_at",
        "price_tick",
        "live_spread_bps",
        "max_spread_bps",
        "spread_acceptable",
    }
)


def _spread_display(
    quote: Quote | None, *, last_price: float | None = None
) -> dict[str, float | bool]:
    if quote is None or quote.bid is None or quote.ask is None:
        return {}
    from trading import execution as execution_mod
    from trading.entry_spread_gate import evaluate_entry_spread

    spread_gate = evaluate_entry_spread(
        quote,
        now=execution_mod._utcnow(),
        tape_last=last_price,
        facts_price=last_price,
    )
    if spread_gate.bps is None:
        return {}
    return {
        "live_spread_bps": round(spread_gate.bps, 1),
        "max_spread_bps": spread_gate.max_bps,
        "spread_acceptable": spread_gate.acceptable,
    }


def price_tick_from_move(prev: float | None, price: float) -> str:
    """up / down / flat from consecutive watch-loop ticks (Alpaca last, not day %)."""
    if prev is None:
        return "flat"
    delta = price - prev
    # Ignore sub-cent / sub-bps noise so the arrow does not flicker.
    if abs(delta) < max(0.01, abs(prev) * 0.00005):
        return "flat"
    return "up" if delta > 0 else "down"


async def refresh_watch_desk_cache(
    watch: EntryWatch,
    *,
    price: float,
    quote: Quote | None,
    md: Any,
    prev_price: float | None = None,
) -> EntryWatch:
    """Compute likelihood (+ arrival when in zone) and store display fields only."""
    bars: list[Bar] = []
    facts: EntryTimingFacts | None = None
    end = datetime.now(UTC)

    in_zone = price_in_zone(
        price,
        watch,
        atr=watch.admission_snapshot.atr_at_creation if watch.admission_snapshot else None,
    ) or watch.status.value in {"triggered", "revalidating"}

    if md is not None and in_zone:
        try:
            bars = await md.get_bars(watch.symbol, Timeframe.H1, end - timedelta(days=60), end)
            if len(bars) >= 30:
                snap = compute_features(watch.symbol, Timeframe.H1, bars)
                facts = evaluate_timing(
                    snap,
                    signal_price=float(watch.signal_price),
                    planned_entry=float(watch.planned_entry),
                    planned_stop=float(watch.planned_stop),
                    planned_target=float(watch.planned_target),
                )
                facts = facts.model_copy(update={"current_price": price})
        except Exception:  # noqa: BLE001 — enrichment must not kill the watch loop
            bars = []

    if facts is None:
        atr = None
        if watch.admission_snapshot and watch.admission_snapshot.atr_at_creation:
            atr = watch.admission_snapshot.atr_at_creation
        facts = EntryTimingFacts(current_price=price, atr=atr)

    full = enrich_watch_for_desk(
        watch,
        price=price,
        facts=facts,
        bars=bars if in_zone else None,
    )
    display: dict[str, object] = {k: full[k] for k in _DISPLAY_KEYS if k in full}
    tick = price_tick_from_move(prev_price, price)
    if tick == "flat":
        # Hold the last non-flat arrow until the tape reverses.
        prior = (watch.desk_enrichment or {}).get("price_tick")
        if prior in {"up", "down"}:
            tick = str(prior)
    display["price_tick"] = tick
    display["enriched_at"] = datetime.now(UTC).isoformat()
    display.update(_spread_display(quote, last_price=price))
    return watch.model_copy(update={"desk_enrichment": display})


def desk_payload(watch: EntryWatch) -> dict[str, Any]:
    """JSON for desk API — machine state from DB row; cache is display-only."""
    base = watch.model_dump(mode="json")
    # Never let cached display fields override machine state.
    machine_keys = {
        "id",
        "symbol",
        "status",
        "state_version",
        "trigger_version",
        "claimed_at",
        "claim_token",
        "claim_owner_id",
        "lease_expires_at",
        "triggered_at",
        "last_admission_record_id",
        "converted_opportunity_id",
        "geometry_hash",
        "exec_timeframe",
        "admission_snapshot",
        "candidate",
        "reasons",
        "valid_until",
        "created_at",
        "last_price",
        "last_observed_at",
    }
    if watch.desk_enrichment:
        for key, value in watch.desk_enrichment.items():
            if key in machine_keys:
                continue
            if key == "desk_enrichment":
                continue
            base[key] = value
    else:
        price = float(watch.last_price or watch.current_price_at_creation)
        facts = EntryTimingFacts(
            current_price=price,
            atr=watch.admission_snapshot.atr_at_creation if watch.admission_snapshot else None,
        )
        enriched = enrich_watch_for_desk(watch, price=price, facts=facts)
        for key, value in enriched.items():
            if key in machine_keys or key == "desk_enrichment":
                continue
            base[key] = value

    # Live mark always wins; re-derive ui_state so a stale TRIGGERED cache cannot
    # claim "in zone" while last_price sits above the band.
    px = float(watch.last_price or watch.current_price_at_creation)
    base["last_price"] = (
        str(watch.last_price) if watch.last_price is not None else base.get("last_price")
    )
    if watch.last_observed_at is not None:
        base["last_observed_at"] = watch.last_observed_at.isoformat().replace("+00:00", "Z")
    # Desk must not stick on revalidating — treat as triggered for display when
    # the mark is still actionable (single-worker lease is an internal lock).
    if watch.status is EntryWatchStatus.REVALIDATING:
        base["status"] = EntryWatchStatus.TRIGGERED.value
    dist = base.get("distance_to_zone_atr")
    try:
        dist_f = float(dist) if dist is not None else None
    except (TypeError, ValueError):
        dist_f = None
    display_watch = watch
    if watch.status is EntryWatchStatus.REVALIDATING:
        display_watch = watch.model_copy(update={"status": EntryWatchStatus.TRIGGERED})
    ui = derive_ui_state(display_watch, price=px, distance_atr=dist_f)
    base["ui_state"] = ui
    base["status_label"] = ui
    atr_v = watch.admission_snapshot.atr_at_creation if watch.admission_snapshot else None
    trig_lo, trig_hi = zone_trigger_bounds(watch, atr=atr_v)
    base["entry_zone_trigger_low"] = round(trig_lo, 4)
    base["entry_zone_trigger_high"] = round(trig_hi, 4)
    in_zone = price_in_zone(px, watch, atr=atr_v)
    if in_zone:
        # Recompute against live entry policy — stale cache must not keep cards
        # blocked after the operator loosens aggressiveness on Settings.
        th = get_entry_thresholds()
        base["buy_blocked"] = buy_blocked_from_arrival_dict(base, th)
        raw = base.get("zone_arrival")
        if isinstance(raw, dict):
            try:
                from trading.zone_arrival import ArrivalType, ZoneArrivalFacts

                arrival = ZoneArrivalFacts(
                    score=float(raw["score"]),
                    arrival_type=ArrivalType(str(raw["arrival_type"])),
                    arrival_speed_pct=raw.get("arrival_speed_pct"),  # type: ignore[arg-type]
                    arrival_speed_atr=raw.get("arrival_speed_atr"),  # type: ignore[arg-type]
                    atr_velocity=raw.get("atr_velocity"),  # type: ignore[arg-type]
                    bars_to_zone=raw.get("bars_to_zone"),  # type: ignore[arg-type]
                    red_bar_ratio=raw.get("red_bar_ratio"),  # type: ignore[arg-type]
                    consecutive_red_bars=int(raw.get("consecutive_red_bars") or 0),
                    largest_red_bar_atr=raw.get("largest_red_bar_atr"),  # type: ignore[arg-type]
                    sell_volume_ratio=raw.get("sell_volume_ratio"),  # type: ignore[arg-type]
                    volume_acceleration=raw.get("volume_acceleration"),  # type: ignore[arg-type]
                    gap_down_pct=raw.get("gap_down_pct"),  # type: ignore[arg-type]
                    crash_velocity=bool(raw.get("crash_velocity")),
                    structural_damage=bool(raw.get("structural_damage")),
                    reason_codes=list(raw.get("reason_codes") or []),
                )
                base["desk_block_reason"] = desk_block_reason_from_arrival(arrival, th)
            except (KeyError, TypeError, ValueError):
                base["desk_block_reason"] = None
        elif not base.get("desk_block_reason"):
            base["desk_block_reason"] = None
    else:
        base["buy_blocked"] = False
        base["desk_block_reason"] = None
        base["zone_arrival"] = None
        base["zone_arrival_quality"] = None
        base["zone_arrival_type"] = None
        base["arrival_reason_codes"] = []
    base["desk_revalidation_hint"] = strip_resolved_spread_hints(
        desk_revalidation_hint(watch),
        spread_acceptable=base.get("spread_acceptable") is True,
    )
    return base
