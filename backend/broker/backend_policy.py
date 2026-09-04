"""Operator execution-broker backend — Alpaca Paper or IBKR Paper.

Persisted like entry policy: Redis when configured, plus a file under data/.
Live is never selectable here; TRAIDO_IBKR_ENV / assert_paper_only stay separate.

Bootstrap: when neither Redis nor file has a value, fall back to TRAIDO_BROKER
(env), then alpaca.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "broker_backend.json"
REDIS_KEY = "traido:broker_backend"
_LOCK = threading.Lock()
_cached: str | None = None

BrokerBackendName = Literal["alpaca", "ibkr"]
ALLOWED: frozenset[str] = frozenset({"alpaca", "ibkr"})


class BrokerBackendError(ValueError):
    """Operator chose an invalid or unsafe backend."""


def _redis_client() -> Any:
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        logger.warning("broker backend: redis unavailable (%s)", type(exc).__name__)
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def normalize_backend(value: str | None) -> BrokerBackendName:
    raw = (value or "").strip().lower()
    if raw in {"", "alpaca", "paper"}:
        return "alpaca"
    if raw == "ibkr":
        return "ibkr"
    if raw == "live":
        raise BrokerBackendError("live broker cannot be selected from the desk")
    raise BrokerBackendError(f"unknown broker backend: {value!r}")


def _env_default() -> BrokerBackendName:
    return normalize_backend(os.getenv("TRAIDO_BROKER"))


def _read_file() -> tuple[str | None, datetime | None, str | None]:
    if not POLICY_PATH.exists():
        return None, None, None
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("broker backend: file read failed (%s)", type(exc).__name__)
        return None, None, None
    try:
        backend = normalize_backend(str(raw.get("backend") or ""))
    except BrokerBackendError:
        return None, None, None
    actor = raw.get("actor")
    return (
        backend,
        _parse_ts(raw.get("updated_at")),
        actor if isinstance(actor, str) else None,
    )


def _read_redis() -> tuple[str | None, datetime | None, str | None]:
    client = _redis_client()
    if client is None:
        return None, None, None
    try:
        raw = client.hget(REDIS_KEY, "backend")
        ts_raw = client.hget(REDIS_KEY, "updated_at")
        actor_raw = client.hget(REDIS_KEY, "actor")
    except Exception as exc:  # noqa: BLE001
        logger.warning("broker backend: redis read failed (%s)", type(exc).__name__)
        return None, None, None
    if raw is None:
        return None, None, None
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(ts_raw, bytes):
        ts_raw = ts_raw.decode()
    if isinstance(actor_raw, bytes):
        actor_raw = actor_raw.decode()
    try:
        actor = actor_raw if isinstance(actor_raw, str) and actor_raw else None
        return normalize_backend(str(raw)), _parse_ts(ts_raw), actor
    except BrokerBackendError:
        return None, None, None


def _heal_redis_from_file(
    backend: str,
    updated_at: datetime | None,
    *,
    actor: str | None,
) -> None:
    ts = updated_at or datetime.now(UTC)
    _write_redis(backend, actor=actor or "user", updated_at=ts.isoformat())


def _write_file(backend: str, *, actor: str, updated_at: str) -> None:
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"backend": backend, "actor": actor, "updated_at": updated_at}
    POLICY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_redis(backend: str, *, actor: str, updated_at: str) -> bool:
    client = _redis_client()
    if client is None:
        return False
    try:
        client.hset(
            REDIS_KEY,
            mapping={"backend": backend, "actor": actor, "updated_at": updated_at},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("broker backend: redis write failed (%s)", type(exc).__name__)
        return False


def _load_backend() -> BrokerBackendName:
    redis_val, redis_ts, redis_actor = _read_redis()
    file_val, file_ts, file_actor = _read_file()

    if redis_actor == "test" and file_val is not None:
        logger.warning(
            "broker backend: ignoring redis test=%s; using file=%s (actor=%s)",
            redis_val,
            file_val,
            file_actor,
        )
        _heal_redis_from_file(file_val, file_ts, actor=file_actor)
        return file_val  # type: ignore[return-value]

    if redis_val is not None and file_val is not None:
        if file_ts is not None and redis_ts is not None:
            return file_val if file_ts >= redis_ts else redis_val  # type: ignore[return-value]
        if file_ts is not None and redis_ts is None:
            return file_val  # type: ignore[return-value]
        if redis_ts is not None and file_ts is None:
            return redis_val  # type: ignore[return-value]
        if redis_val != file_val:
            logger.warning(
                "broker backend: redis=%s file=%s disagree without updated_at; preferring file",
                redis_val,
                file_val,
            )
            return file_val  # type: ignore[return-value]
        return redis_val  # type: ignore[return-value]
    if redis_val is not None:
        return redis_val  # type: ignore[return-value]
    if file_val is not None:
        return file_val  # type: ignore[return-value]
    return _env_default()


def get_broker_backend() -> BrokerBackendName:
    global _cached
    with _LOCK:
        if _cached is None:
            _cached = _load_backend()
        return _cached  # type: ignore[return-value]


def set_broker_backend(value: str, *, actor: str = "user") -> BrokerBackendName:
    """Persist selection and return the normalized backend name."""
    global _cached
    backend = normalize_backend(value)
    updated_at = datetime.now(UTC).isoformat()
    _write_file(backend, actor=actor, updated_at=updated_at)
    wrote_redis = _write_redis(backend, actor=actor, updated_at=updated_at)
    with _LOCK:
        _cached = backend
    logger.info(
        "broker backend: backend=%s actor=%s redis=%s",
        backend,
        actor,
        "ok" if wrote_redis else "skip",
    )
    return backend


def reset_broker_backend_cache() -> None:
    """Tests: drop the in-memory cache so the next read hits Redis/file/env."""
    global _cached
    with _LOCK:
        _cached = None


def broker_backend_payload() -> dict[str, Any]:
    backend = get_broker_backend()
    return {
        "backend": backend,
        "environment": "paper",
        "note": (
            "Execution venue only — market data stays on Alpaca. "
            "IBKR Paper needs Gateway at TRAIDO_IBKR_HOST (Mac via Tailscale on Railway — see docs/deploy/ibkr-gateway.md)."
        ),
    }
