"""Durable Paper-only automatic exit switch (OFF: targets/operator only).

None preserves the per-position policy until the operator first changes it.
Redis is authoritative on Railway; a failed durable write never changes memory.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "auto_stop.json"
REDIS_KEY = "traido:auto_stop:v1"
_LOCK = threading.RLock()
_loaded = False
_override: bool | None = None


def _redis_client() -> Any:
    import redis

    return redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=1, socket_connect_timeout=1)


def _decode(raw: str | bytes | None) -> bool | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("AUTO_STOP_POLICY_INVALID")
    return bool(value["enabled"])


def get_override() -> bool | None:
    global _loaded, _override
    with _LOCK:
        if not _loaded:
            if os.getenv("REDIS_URL"):
                value = _decode(_redis_client().get(REDIS_KEY))
            else:
                value = _decode(POLICY_PATH.read_text() if POLICY_PATH.exists() else None)
            _override = value
            _loaded = True
        return _override


def set_enabled(enabled: bool) -> None:
    global _loaded, _override
    from core.config import get_settings
    from core.enums import BrokerEnvironment

    if get_settings().broker_env is not BrokerEnvironment.PAPER:
        raise ValueError("AUTO_STOP_PAPER_ONLY")
    if type(enabled) is not bool:
        raise ValueError("AUTO_STOP_POLICY_INVALID")
    raw = json.dumps(
        {"enabled": enabled, "actor": "user", "updated_at": datetime.now(UTC).isoformat()}
    )
    with _LOCK:
        if os.getenv("REDIS_URL"):
            if not _redis_client().set(REDIS_KEY, raw):
                raise RuntimeError("AUTO_STOP_POLICY_NOT_SAVED")
        else:
            POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = POLICY_PATH.with_suffix(".tmp")
            temporary.write_text(raw + "\n")
            temporary.replace(POLICY_PATH)
        _override, _loaded = enabled, True


def reset_cache() -> None:
    global _loaded, _override
    with _LOCK:
        _loaded, _override = False, None


def policy_payload() -> dict[str, Any]:
    from core.config import get_settings
    from core.enums import BrokerEnvironment

    available = get_settings().broker_env is BrokerEnvironment.PAPER
    value = get_override() if available else None
    return {"enabled": value is not False, "configured": value is not None, "available": available}
