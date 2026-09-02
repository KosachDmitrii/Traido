"""Apply desk enrichment during watch loop passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.enums import Timeframe
from core.schemas import Bar, EntryTimingFacts, EntryWatch, Quote
from quant.engine import compute_features
from trading.entry_timing import evaluate_timing
from trading.watch_desk import enrich_watch_for_desk

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
        "zone_touch_calibration",
        "enriched_at",
    }
)


async def refresh_watch_desk_cache(
    watch: EntryWatch,
    *,
    price: float,
    quote: Quote | None,
    md: Any,
) -> EntryWatch:
    """Compute likelihood (+ arrival when in zone) and store display fields only."""
    del quote  # quote reserved for future spread display; machine state ignores it
    bars: list[Bar] = []
    facts: EntryTimingFacts | None = None
    end = datetime.now(UTC)

    in_zone = (
        float(watch.entry_zone_low) <= price <= float(watch.entry_zone_high)
        or watch.status.value in {"triggered", "revalidating"}
    )

    if md is not None and in_zone:
        try:
            bars = await md.get_bars(
                watch.symbol, Timeframe.H1, end - timedelta(days=60), end
            )
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
    display["enriched_at"] = datetime.now(UTC).isoformat()
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
    return base
