"""Immutable geometry fingerprint — entry/stop/target/timeframe/version."""

from __future__ import annotations

import hashlib
import json

from core.schemas import EntryWatch, TradeCandidate


def compute_geometry_hash(
    *,
    entry: float | str,
    stop: float | str,
    target: float | str | None,
    exec_timeframe: str = "H1",
    strategy_version: str = "",
) -> str:
    payload = {
        "entry": round(float(entry), 4),
        "stop": round(float(stop), 4),
        "target": round(float(target), 4) if target is not None else None,
        "exec_timeframe": exec_timeframe.upper(),
        "strategy_version": strategy_version,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def geometry_hash_from_watch(watch: EntryWatch) -> str:
    """Hash for an EntryWatch planned geometry."""
    return compute_geometry_hash(
        entry=float(watch.planned_entry),
        stop=float(watch.planned_stop),
        target=float(watch.planned_target),
        exec_timeframe=getattr(watch, "exec_timeframe", None) or "H1",
        strategy_version=watch.strategy_version or "",
    )


def geometry_hash_from_candidate(candidate: TradeCandidate, *, exec_timeframe: str = "H1") -> str:
    if candidate.exit_policy == "session_close":
        payload = {
            "entry": str(candidate.entry),
            "stop": str(candidate.stop),
            "exit_at": candidate.exit_at.isoformat() if candidate.exit_at else None,
            "exit_policy": candidate.exit_policy,
            "version": candidate.strategy_version,
            "plan": candidate.orb_plan,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
    return compute_geometry_hash(
        entry=float(candidate.entry),
        stop=float(candidate.stop),
        target=float(candidate.target) if candidate.target is not None else None,
        exec_timeframe=exec_timeframe,
        strategy_version=candidate.strategy_version or "",
    )
