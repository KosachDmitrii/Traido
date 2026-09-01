"""
Process-wide kill switch.

The kill switch is the last thing between a misbehaving desk and the account,
so it has to satisfy three properties that a plain in-memory flag does not:

1. **Durable** — surviving a restart. A crash must not silently re-arm trading.
2. **Shared** — every worker, the scanner task, and the API must see the same
   state. Two processes disagreeing about whether trading is halted is the
   worst possible outcome.
3. **Fail-closed on read ambiguity** — if the shared store is unreachable we
   fall back to the local file flag rather than reporting "off". Reporting
   "off" when we do not know is how accounts get emptied.

Redis is the shared store when `REDIS_URL` is configured; the file flag is
always written too, so a Redis outage degrades to single-node correctness
instead of no protection at all.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FLAG = Path(__file__).resolve().parents[1] / "data" / "kill_switch.on"
REDIS_KEY = "traido:kill_switch"


@dataclass(frozen=True)
class KillSwitchState:
    enabled: bool
    source: str
    """Where the answer came from: redis, file, or degraded."""
    changed_at: str | None = None
    actor: str | None = None
    reason: str | None = None


def _redis_client() -> Any:
    """Synchronous Redis client, or None when Redis is not configured/available."""
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 — any Redis problem means "use the file"
        logger.warning("kill switch: redis unavailable (%s)", type(exc).__name__)
        return None


def _file_enabled() -> bool:
    return FLAG.exists()


def _read_file_state() -> tuple[bool, dict[str, str]]:
    """Flag presence plus whatever provenance we can recover from it.

    Presence alone means "halted" — an operator who panics and runs
    `touch data/kill_switch.on` must stop the desk even though the file has no
    readable metadata.
    """
    if not FLAG.exists():
        return False, {}
    try:
        meta = json.loads(FLAG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, {}
    if not isinstance(meta, dict):
        return True, {}
    return True, {str(k): str(v) for k, v in meta.items()}


def _write_file(enabled: bool, meta: dict[str, str]) -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        FLAG.write_text(json.dumps(meta), encoding="utf-8")
    elif FLAG.exists():
        FLAG.unlink()


def get_kill_switch_state() -> KillSwitchState:
    """Full state including provenance — used by health checks and the desk UI."""
    file_on, file_meta = _read_file_state()

    client = _redis_client()
    if client is None:
        return _from_file(file_on, file_meta, os.getenv("REDIS_URL") is None)

    try:
        raw = client.hgetall(REDIS_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kill switch: redis read failed (%s)", type(exc).__name__)
        return _from_file(file_on, file_meta, redis_configured=False)

    if not raw:
        # Redis has never been written. The file is still authoritative so an
        # engaged switch is not lost when Redis is introduced or flushed.
        return _from_file(file_on, file_meta, redis_configured=True)

    decoded = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }
    if decoded.get("enabled") == "1":
        return KillSwitchState(
            enabled=True,
            source="redis",
            changed_at=decoded.get("changed_at"),
            actor=decoded.get("actor"),
            reason=decoded.get("reason"),
        )

    # Redis says released. The file still wins if it says halted, and its own
    # provenance is what explains the halt.
    if file_on:
        return _from_file(True, file_meta, redis_configured=True)

    return KillSwitchState(
        enabled=False,
        source="redis",
        changed_at=decoded.get("changed_at"),
        actor=decoded.get("actor"),
        reason=decoded.get("reason"),
    )


def _from_file(
    enabled: bool, meta: dict[str, str], redis_configured: bool = True
) -> KillSwitchState:
    return KillSwitchState(
        enabled=enabled,
        source="file" if redis_configured else "degraded",
        changed_at=meta.get("changed_at"),
        actor=meta.get("actor"),
        reason=meta.get("reason"),
    )


def is_kill_switch_on() -> bool:
    return get_kill_switch_state().enabled


def set_kill_switch(
    enabled: bool,
    *,
    actor: str = "system",
    reason: str = "",
) -> bool:
    now = datetime.now(UTC).isoformat()
    _write_file(enabled, {"changed_at": now, "actor": actor, "reason": reason})

    client = _redis_client()
    if client is not None:
        try:
            client.hset(
                REDIS_KEY,
                mapping={
                    "enabled": "1" if enabled else "0",
                    "changed_at": now,
                    "actor": actor,
                    "reason": reason,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("kill switch: redis write failed (%s)", type(exc).__name__)

    logger.warning(
        "kill switch %s by %s%s",
        "ENGAGED" if enabled else "released",
        actor,
        f": {reason}" if reason else "",
    )
    return enabled
