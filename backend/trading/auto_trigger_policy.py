"""Desk auto-buy on TRIGGERED→ADMITTED path — operator toggle (paper confirmation desk).

Auto-approve is forbidden while ``TRAIDO_TRADING_MODE=confirmation`` (the default).
Human Buy is the only approval path until automatic trading mode is implemented.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "auto_trigger.json"
REDIS_KEY = "traido:auto_trigger"
_LOCK = threading.Lock()
_cached: bool | None = None


def _confirmation_mode_blocks_auto_trigger() -> bool:
    from core.config import get_settings
    from core.enums import TradingMode

    return get_settings().trading_mode is TradingMode.CONFIRMATION


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
        logger.warning("auto trigger: redis unavailable (%s)", type(exc).__name__)
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _read_file() -> tuple[bool | None, datetime | None, str | None]:
    if not POLICY_PATH.exists():
        return None, None, None
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("auto trigger: unreadable file")
        return None, None, None
    if isinstance(raw, dict):
        actor = raw.get("actor")
        return (
            bool(raw.get("enabled", False)),
            _parse_ts(raw.get("updated_at")),
            actor if isinstance(actor, str) else None,
        )
    return None, None, None


def _read_redis() -> tuple[bool | None, datetime | None, str | None]:
    client = _redis_client()
    if client is None:
        return None, None, None
    try:
        raw = client.hget(REDIS_KEY, "enabled")
        ts_raw = client.hget(REDIS_KEY, "updated_at")
        actor_raw = client.hget(REDIS_KEY, "actor")
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto trigger: redis read failed (%s)", type(exc).__name__)
        return None, None, None
    if raw is None:
        return None, None, None
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(ts_raw, bytes):
        ts_raw = ts_raw.decode()
    if isinstance(actor_raw, bytes):
        actor_raw = actor_raw.decode()
    enabled = str(raw).strip() in {"1", "true", "True", "yes"}
    actor = actor_raw if isinstance(actor_raw, str) and actor_raw else None
    return enabled, _parse_ts(ts_raw), actor


def _write_file(enabled: bool, *, actor: str, updated_at: str) -> None:
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"enabled": enabled, "actor": actor, "updated_at": updated_at}
    POLICY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_redis(enabled: bool, *, actor: str, updated_at: str) -> bool:
    client = _redis_client()
    if client is None:
        return False
    try:
        client.hset(
            REDIS_KEY,
            mapping={"enabled": "1" if enabled else "0", "actor": actor, "updated_at": updated_at},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("auto trigger: redis write failed (%s)", type(exc).__name__)
        return False


def _heal_redis_from_file(
    enabled: bool,
    updated_at: datetime | None,
    *,
    actor: str | None,
) -> None:
    ts = updated_at or datetime.now(UTC)
    _write_redis(enabled, actor=actor or "user", updated_at=ts.isoformat())


def _load_enabled() -> bool:
    """Prefer the newer of Redis vs file; ignore test snapshots over operator file."""
    if _confirmation_mode_blocks_auto_trigger():
        return False
    redis_val, redis_ts, redis_actor = _read_redis()
    file_val, file_ts, file_actor = _read_file()

    if redis_actor == "test" and file_val is not None:
        logger.warning(
            "auto trigger: ignoring redis test=%s; using file=%s (actor=%s)",
            redis_val,
            file_val,
            file_actor,
        )
        _heal_redis_from_file(file_val, file_ts, actor=file_actor)
        return file_val

    if redis_val is not None and file_val is not None:
        if file_ts is not None and redis_ts is not None:
            return file_val if file_ts >= redis_ts else redis_val
        if file_ts is not None and redis_ts is None:
            return file_val
        if redis_ts is not None and file_ts is None:
            return redis_val
        if redis_val != file_val:
            logger.warning(
                "auto trigger: redis=%s file=%s disagree without updated_at; preferring file",
                redis_val,
                file_val,
            )
            return file_val
        return redis_val
    if redis_val is not None:
        return redis_val
    if file_val is not None:
        return file_val
    return False


def get_auto_trigger_enabled() -> bool:
    global _cached
    with _LOCK:
        if _cached is None:
            _cached = _load_enabled()
        return _cached


def set_auto_trigger_enabled(value: bool, *, actor: str = "user") -> bool:
    global _cached
    if value and _confirmation_mode_blocks_auto_trigger():
        logger.warning("auto trigger: cannot enable while trading_mode=confirmation")
        with _LOCK:
            _cached = False
        return False
    enabled = bool(value)
    updated_at = datetime.now(UTC).isoformat()
    _write_file(enabled, actor=actor, updated_at=updated_at)
    wrote_redis = _write_redis(enabled, actor=actor, updated_at=updated_at)
    with _LOCK:
        _cached = enabled
    logger.info(
        "auto trigger: enabled=%s actor=%s redis=%s",
        enabled,
        actor,
        "ok" if wrote_redis else "skip",
    )
    return enabled


def reset_auto_trigger_cache() -> None:
    global _cached
    with _LOCK:
        _cached = None


def policy_payload() -> dict[str, Any]:
    blocked = _confirmation_mode_blocks_auto_trigger()
    return {
        "enabled": get_auto_trigger_enabled(),
        "available": not blocked,
        "note": (
            "Auto-approve is disabled in confirmation mode — use Buy on the card."
            if blocked
            else (
                "When on, ADMITTED watches that publish a BUY card are auto-approved "
                "through ExecutionService (kill switch, RTH, reconciliation, liquidity, "
                "risk — same as manual Buy). Paper execution only."
            )
        ),
    }


async def maybe_auto_approve_opportunity(
    opportunity_id: Any,
    *,
    audit: Any,
    symbol: str,
) -> bool:
    """Return True when an approve decision was attempted and succeeded."""
    if _confirmation_mode_blocks_auto_trigger():
        return False
    if not get_auto_trigger_enabled():
        return False

    from uuid import UUID, uuid4

    from core.enums import UserDecision
    from trading.opportunities import OPPORTUNITIES

    opp_id = opportunity_id if isinstance(opportunity_id, UUID) else UUID(str(opportunity_id))
    opp = OPPORTUNITIES.get(opp_id)
    if opp is None:
        return False

    from api.deps import build_execution_service

    service = build_execution_service()
    request_id = uuid4()
    try:
        result = await service.decide(
            opp_id,
            UserDecision.APPROVE,
            request_id=request_id,
            expected_decision_version=opp.decision_version,
        )
    except Exception as exc:  # noqa: BLE001 — desk must continue on auto-buy failure
        logger.warning("auto trigger: approve failed for %s (%s)", symbol, exc)
        await audit.append(
            "AutoTriggerApproveFailed",
            "auto_trigger",
            {"opportunity_id": str(opp_id), "symbol": symbol, "error": type(exc).__name__},
        )
        return False

    await audit.append(
        "AutoTriggerApproved",
        "auto_trigger",
        {
            "opportunity_id": str(opp_id),
            "symbol": symbol,
            "status": result.status.value,
            "request_id": str(request_id),
        },
    )
    from core.desk_bus import DESK_BUS

    DESK_BUS.bump_desk(kind="auto_trigger_approve", symbol=symbol)
    DESK_BUS.bump_broker(kind="auto_trigger_approve")
    return True
