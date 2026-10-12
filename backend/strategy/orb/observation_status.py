"""Presentation/telemetry only; observation age never grants entry permission."""

from collections import Counter
from datetime import datetime, timedelta
from typing import Any


def observation_status(session: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    plans = session.get("plans") or {}
    states = session.get("states") or {}
    checked = 0
    reasons: Counter[str] = Counter()
    for symbol in plans:
        state = states.get(symbol) or {}
        try:
            stamp = datetime.fromisoformat(
                state.get("last_checked_at") or state.get("observed_at") or ""
            )
            if stamp.tzinfo is not None and now - timedelta(minutes=5) <= stamp <= now:
                checked += 1
        except (TypeError, ValueError):
            pass
        if state.get("state") == "DATA_BLOCKED":
            codes = state.get("reasons") or ["UNKNOWN"]
            reasons.update(set(codes))
    return {
        "total": len(plans),
        "checked_recently": checked,
        "pending": len(plans) - checked,
        "blocked_reasons": dict(reasons),
    }


def next_observation_at(
    session: dict[str, Any], *, now: datetime, reserve_seconds: float
) -> datetime | None:
    """Start the oldest-first portion before its oldest check expires.

    Reserve the last portion's measured runtime. Missing, invalid or future
    stamps require immediate observation; an empty universe has no deadline.
    This scheduling deadline cannot grant admission or override vendor pacing.
    """
    deadlines = []
    for symbol in session.get("plans") or {}:
        state = (session.get("states") or {}).get(symbol) or {}
        try:
            stamp = datetime.fromisoformat(
                state.get("last_checked_at") or state.get("observed_at") or ""
            )
            if stamp.tzinfo is None or stamp > now:
                return now
        except (TypeError, ValueError):
            return now
        deadlines.append(stamp + timedelta(minutes=5))
    if not deadlines:
        return None
    return max(now, min(deadlines) - timedelta(seconds=max(0, reserve_seconds)))
