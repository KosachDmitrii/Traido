"""Downsampled watch telemetry for calibration — not every 30s tick."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.schemas import EntryWatch

_LOCK = threading.Lock()
_PATH = Path(__file__).resolve().parents[1] / "data" / "watch_telemetry.jsonl"
_MIN_INTERVAL_SEC = 300
_MIN_PRICE_MOVE_PCT = 0.15
_last_written: dict[str, tuple[float, datetime]] = {}


def record_watch_telemetry(
    watch: EntryWatch,
    *,
    price: float,
    enrichment: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Append one row if price moved enough or TTL window elapsed."""
    now = now or datetime.now(UTC)
    key = str(watch.id)
    with _LOCK:
        prev = _last_written.get(key)
        if prev is not None:
            last_px, last_ts = prev
            elapsed = (now - last_ts).total_seconds()
            move_pct = abs(price - last_px) / max(abs(last_px), 1e-9) * 100.0
            if elapsed < _MIN_INTERVAL_SEC and move_pct < _MIN_PRICE_MOVE_PCT:
                return False

    desk: dict[str, Any] = dict(enrichment or watch.desk_enrichment or {})
    row = {
        "watch_id": str(watch.id),
        "symbol": watch.symbol.upper(),
        "recorded_at": now.isoformat(),
        "last_price": round(price, 4),
        "status": watch.status.value,
        "setup_type": watch.setup_type.value if watch.setup_type else None,
        "distance_atr": desk.get("distance_to_zone_atr"),
        "entry_likelihood": (desk.get("entry_likelihood") or {}).get("classification"),
        "zone_arrival_quality": desk.get("zone_arrival_quality"),
        "ui_state": desk.get("ui_state"),
    }
    with _LOCK:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        _last_written[key] = (price, now)
    return True


def clear_telemetry_cache() -> None:
    """Tests only."""
    with _LOCK:
        _last_written.clear()
