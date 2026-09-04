"""Desk auto-buy — operator toggle (paper confirmation desk).

When enabled, every new BUY card is approved through ``ExecutionService`` —
same gates as the Buy button. Transient refusals keep the card and retry;
only a terminal reject discards it. Paper broker env only; live refuses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "auto_trigger.json"
REDIS_KEY = "traido:auto_trigger"
_LOCK = threading.Lock()
_cached: bool | None = None
_in_flight: set[str] = set()
_retry_after: dict[str, datetime] = {}
_retry_attempts: dict[str, int] = {}
_queue: asyncio.Queue[tuple[Any, Any, str]] | None = None
_worker_started = False

_BACKOFF_STEPS = (5, 15, 45, 120, 300)


def _auto_trigger_blocked() -> bool:
    """Paper-only unattended approve — live broker env is refused."""
    from core.config import get_settings
    from core.enums import BrokerEnvironment

    return get_settings().broker_env is BrokerEnvironment.LIVE


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
    if _auto_trigger_blocked():
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
    if value and _auto_trigger_blocked():
        logger.warning("auto trigger: cannot enable while broker_env=live")
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
        _retry_after.clear()
        _retry_attempts.clear()
        _in_flight.clear()


def policy_payload() -> dict[str, Any]:
    blocked = _auto_trigger_blocked()
    return {
        "enabled": get_auto_trigger_enabled(),
        "available": not blocked,
        "note": (
            "Auto-approve is disabled on live broker — switch to paper or use Buy on the card."
            if blocked
            else (
                "When on, a BUY card is auto-approved through ExecutionService "
                "(kill switch, RTH, reconciliation, liquidity, risk — same as "
                "manual Buy). A terminal reject discards the card; data or "
                "operational blocks retry. Paper only."
            )
        ),
    }


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _classify_failure(exc: BaseException) -> str:
    from trading.approval_errors import DataBlockedError, NoTradeError, WaitError
    from trading.outcome_taxonomy import OutcomeClass, classify_exception_text

    if isinstance(exc, DataBlockedError):
        return OutcomeClass.DATA_BLOCKED.value
    if isinstance(exc, NoTradeError):
        return OutcomeClass.NO_TRADE.value
    if isinstance(exc, WaitError):
        return OutcomeClass.WAIT.value
    return classify_exception_text(_error_text(exc)).value


def _set_retry(opportunity_id: Any, *, operational: bool) -> datetime:
    key = str(opportunity_id)
    with _LOCK:
        attempt = _retry_attempts.get(key, 0) + 1
        _retry_attempts[key] = attempt
        delay = _BACKOFF_STEPS[min(attempt - 1, len(_BACKOFF_STEPS) - 1)]
        if not operational:
            delay = min(delay, 30)
        until = datetime.now(UTC) + timedelta(seconds=delay)
        _retry_after[key] = until
        return until


def _clear_retry(opportunity_id: Any) -> None:
    key = str(opportunity_id)
    with _LOCK:
        _retry_after.pop(key, None)
        _retry_attempts.pop(key, None)


def _due_for_retry(opportunity_id: Any) -> bool:
    key = str(opportunity_id)
    with _LOCK:
        until = _retry_after.get(key)
    return until is None or datetime.now(UTC) >= until


async def _discard_card(
    opportunity_id: Any,
    *,
    audit: Any,
    symbol: str,
    error: str,
    outcome: str,
) -> None:
    from core.desk_bus import DESK_BUS
    from core.enums import OpportunityStatus
    from trading.opportunities import OPPORTUNITIES

    claimed = OPPORTUNITIES.claim(
        opportunity_id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.DISCARDED,
    )
    _clear_retry(opportunity_id)
    await audit.append(
        "AutoTriggerApproveFailed",
        "auto_trigger",
        {
            "opportunity_id": str(opportunity_id),
            "symbol": symbol,
            "error": error,
            "outcome": outcome,
        },
    )
    if claimed is not None:
        await audit.append(
            "OpportunityDiscarded",
            "auto_trigger",
            {
                "opportunity_id": str(opportunity_id),
                "symbol": symbol,
                "reason": "auto_trigger_terminal_reject",
                "error": error,
                "outcome": outcome,
            },
        )
    DESK_BUS.bump_desk(
        kind="auto_trigger_failed",
        symbol=symbol,
        opportunity_id=str(opportunity_id),
        error=error,
        outcome=outcome,
    )
    DESK_BUS.bump_broker(kind="auto_trigger_failed")


async def _keep_card(
    opportunity_id: Any,
    *,
    audit: Any,
    symbol: str,
    error: str,
    outcome: str,
) -> None:
    from core.desk_bus import DESK_BUS

    until = _set_retry(opportunity_id, operational=outcome == "OPERATIONAL_BLOCKED")
    await audit.append(
        "AutoTriggerApproveDeferred",
        "auto_trigger",
        {
            "opportunity_id": str(opportunity_id),
            "symbol": symbol,
            "error": error,
            "outcome": outcome,
            "retry_at": until.isoformat(),
        },
    )
    DESK_BUS.bump_desk(
        kind="auto_trigger_deferred",
        symbol=symbol,
        opportunity_id=str(opportunity_id),
        error=error,
        outcome=outcome,
        retry_at=until.isoformat(),
    )


async def maybe_auto_approve_opportunity(
    opportunity_id: Any,
    *,
    audit: Any,
    symbol: str,
) -> bool:
    """Return True when an approve decision was attempted and succeeded."""
    if _auto_trigger_blocked():
        return False
    if not get_auto_trigger_enabled():
        return False

    from uuid import UUID, uuid4

    from core.enums import OpportunityStatus, UserDecision
    from trading.opportunities import OPPORTUNITIES

    opp_id = opportunity_id if isinstance(opportunity_id, UUID) else UUID(str(opportunity_id))
    key = str(opp_id)
    with _LOCK:
        if key in _in_flight:
            return False
        _in_flight.add(key)
    try:
        opp = OPPORTUNITIES.get(opp_id)
        if opp is None or opp.status is not OpportunityStatus.AWAITING_CONFIRMATION:
            return False
        if not _due_for_retry(opp_id):
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
        except Exception as exc:  # noqa: BLE001 — classify; never skip a transient
            error = _error_text(exc)
            outcome = _classify_failure(exc)
            logger.warning(
                "auto trigger: approve failed for %s outcome=%s (%s)",
                symbol,
                outcome,
                error,
            )
            if outcome in {"NO_TRADE", "TERMINAL_REJECT"}:
                await _discard_card(
                    opp_id, audit=audit, symbol=symbol, error=error, outcome=outcome
                )
            elif outcome == "UNKNOWN":
                await audit.append(
                    "AutoTriggerStateUnknown",
                    "auto_trigger",
                    {
                        "opportunity_id": str(opp_id),
                        "symbol": symbol,
                        "error": error,
                        "outcome": outcome,
                    },
                )
            else:
                await _keep_card(opp_id, audit=audit, symbol=symbol, error=error, outcome=outcome)
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

        DESK_BUS.bump_desk(
            kind="auto_trigger_approve",
            symbol=symbol,
            status=result.status.value,
        )
        DESK_BUS.bump_broker(kind="auto_trigger_approve")
        _clear_retry(opp_id)
        return True
    finally:
        with _LOCK:
            _in_flight.discard(key)


def _ensure_worker() -> asyncio.Queue[tuple[Any, Any, str]] | None:
    global _queue, _worker_started
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _queue is None:
        _queue = asyncio.Queue()
    if not _worker_started:
        _worker_started = True
        loop.create_task(_auto_trigger_worker())
    return _queue


async def _auto_trigger_worker() -> None:
    assert _queue is not None
    while True:
        opportunity_id, audit, symbol = await _queue.get()
        try:
            await maybe_auto_approve_opportunity(opportunity_id, audit=audit, symbol=symbol)
        except Exception:
            logger.exception("auto trigger worker failed for %s", symbol)
        finally:
            _queue.task_done()


def enqueue_auto_approve_opportunity(
    opportunity_id: Any,
    *,
    audit: Any,
    symbol: str,
) -> bool:
    """Queue an approve. Caller returns immediately — decide() runs off-cycle."""
    if _auto_trigger_blocked() or not get_auto_trigger_enabled():
        return False
    queue = _ensure_worker()
    if queue is None:
        return False
    queue.put_nowait((opportunity_id, audit, symbol))
    return True


def enqueue_auto_approve_open_buys(*, audit: Any) -> int:
    """Queue every open BUY card without waiting for fills."""
    if _auto_trigger_blocked() or not get_auto_trigger_enabled():
        return 0
    from trading.opportunities import OPPORTUNITIES

    queued = 0
    for opp in list(OPPORTUNITIES.list_open()):
        if enqueue_auto_approve_opportunity(
            opp.id,
            audit=audit,
            symbol=opp.candidate.symbol,
        ):
            queued += 1
    return queued


async def maybe_auto_approve_open_buys(*, audit: Any) -> int:
    """Approve every open BUY card. Tests may await this; the desk enqueues."""
    return enqueue_auto_approve_open_buys(audit=audit)
