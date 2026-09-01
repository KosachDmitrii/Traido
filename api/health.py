"""
Health and readiness.

`/health` is a liveness probe: it answers "is this process up" and must never
touch a dependency, or a slow database will get the container killed.

`/health/ready` is the readiness probe: it actually checks the things the desk
cannot work without and reports each one separately, so an outage points at a
component instead of just going red. Readiness fails only on hard dependencies
(database); a missing news vendor is degraded, not down.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from core.config import Settings

CHECK_TIMEOUT = 3.0


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str = ""
    latency_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
        }


@dataclass
class ReadinessReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def degraded(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok and not c.required]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "degraded": self.degraded,
            "checks": {c.name: c.as_dict() for c in self.checks},
        }


async def _timed(name: str, required: bool, coro) -> Check:  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    try:
        detail = await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT)
        ok = True
    except TimeoutError:
        detail, ok = f"timed out after {CHECK_TIMEOUT}s", False
    except Exception as exc:  # noqa: BLE001 — a health check must never raise
        detail, ok = f"{type(exc).__name__}: {exc}"[:200], False
    return Check(
        name=name,
        ok=ok,
        required=required,
        detail=str(detail),
        latency_ms=(time.perf_counter() - start) * 1000,
    )


async def _check_database() -> str:
    from sqlalchemy import text

    from database.session import get_sync_engine

    engine = get_sync_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return engine.dialect.name


async def _check_redis() -> str:
    url = os.getenv("REDIS_URL")
    if not url:
        return "not configured"

    import redis.asyncio as aioredis

    client = aioredis.from_url(url, socket_timeout=2.0)
    try:
        await client.ping()
    finally:
        await client.aclose()
    return "reachable"


async def _check_broker(settings: Settings) -> str:
    from broker.factory import create_broker

    broker = create_broker(settings)
    if broker.environment != "paper":
        raise RuntimeError(f"broker environment is {broker.environment!r}, expected 'paper'")
    await broker.get_portfolio()
    return "paper account reachable"


async def _check_market_data(settings: Settings) -> str:
    from market_data.factory import create_market_data_port

    port = create_market_data_port(settings)
    await port.get_last_price("SPY")
    return type(port).__name__


async def build_readiness(settings: Settings) -> ReadinessReport:
    checks = await asyncio.gather(
        _timed("database", True, _check_database()),
        _timed("redis", False, _check_redis()),
        _timed("broker", False, _check_broker(settings)),
        _timed("market_data", False, _check_market_data(settings)),
    )
    return ReadinessReport(checks=list(checks))
