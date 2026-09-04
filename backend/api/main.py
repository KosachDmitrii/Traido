"""FastAPI entrypoint — Confirmation desk API (UI is Vite React on :3000)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response

from agents.position.loop import start_position_loop, stop_position_loop
from agents.scanner.agent import start_scanner, stop_scanner
from api.auth import ApiAuthMiddleware, assert_auth_configured, auth_mode, is_development
from api.deps import build_exit_assessment, build_reconcile_pass
from api.health import build_readiness
from api.routes.desk import router as desk_router
from api.routes.evaluation import router as evaluation_router
from api.routes.logs import router as logs_router
from api.routes.review import router as review_router
from api.routes.scan import router as scan_router
from api.routes.strategies import router as strategies_router
from api.routes.trading import router as trading_router
from core.activity import bind_activity_audit
from core.audit import create_audit
from core.config import get_settings
from core.deployment import assert_implemented_trading_mode, assert_single_worker
from core.desk_bus import DESK_BUS
from core.log_retention import prune_audit_events, start_log_retention, stop_log_retention
from core.logging import configure_logging, get_logger
from database.session import init_db
from risk.kill_switch import get_kill_switch_state
from risk.limits import default_risk_limits
from trading.entry_watch_loop import start_entry_watch_loop, stop_entry_watch_loop
from trading.reconcile_supervisor import start_reconcile_loop, stop_reconcile_loop

settings = get_settings()
DASHBOARD_URL = os.getenv("TRAIDO_DASHBOARD_URL", "http://127.0.0.1:3000")

logger = get_logger(__name__)


def allowed_origins() -> list[str]:
    """
    CORS origins.

    Defaults to the local Vite desk. `TRAIDO_CORS_ORIGINS` (comma separated)
    overrides it for a deployed frontend. Wildcards are deliberately not
    supported: the API is credentialed and `*` would let any page call it.
    """
    raw = os.getenv("TRAIDO_CORS_ORIGINS")
    if not raw:
        return ["http://127.0.0.1:3000", "http://localhost:3000"]
    origins = [o.strip() for o in raw.split(",") if o.strip() and o.strip() != "*"]
    return origins or ["http://127.0.0.1:3000"]


def _warn_if_entries_are_blocked_by_config() -> None:
    """Say at boot what would otherwise only be discovered from a silent desk.

    A configuration that refuses every entry is a legitimate state — refusing is
    the safe direction — but it looks exactly like a scanner that found nothing
    worth proposing. The difference belongs in the first lines of the log, not in
    an operator's guess an hour later.
    """
    if default_risk_limits().require_earnings_check and not settings.finnhub_api_key:
        logger.warning(
            "FINNHUB_API_KEY is not set: the earnings calendar cannot be read, so "
            "every new entry will be refused with EARNINGS_CALENDAR_NOT_CONFIGURED. "
            "Set the key, or set risk_limits_v1.require_earnings_check=false in "
            "configs/v1_paper.json to trade with event risk deliberately unchecked."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    assert_auth_configured()
    assert_single_worker()
    assert_implemented_trading_mode()
    init_db()
    from strategy.registry import ensure_builtin_strategies

    ensure_builtin_strategies()
    from trading.entry_watch_persistence import (
        configure_entry_watch_persistence,
        hydrate_entry_watches,
        patch_entry_watch_store,
    )
    from trading.entry_watches import ENTRY_WATCHES

    configure_entry_watch_persistence(enabled=True)
    patch_entry_watch_store(ENTRY_WATCHES)
    hydrate_entry_watches()
    audit = create_audit()
    bind_activity_audit(audit)
    retention_stop, retention_task = start_log_retention(audit)
    await asyncio.to_thread(prune_audit_events, audit)
    logger.info(
        "Traido API starting",
        extra={
            "environment": settings.environment,
            "broker_env": settings.broker_env.value,
            "trading_mode": settings.trading_mode.value,
            "auth_mode": auth_mode(),
        },
    )
    _warn_if_entries_are_blocked_by_config()
    DESK_BUS.reopen()
    start_scanner()
    # Broker truth is re-read on a timer rather than when a browser asks for it.
    # Whether the desk knows what it is holding must not depend on whether
    # anyone is looking at the dashboard — overnight, nobody is.
    start_reconcile_loop(build_reconcile_pass)
    # And whether an open position should still be held. Same reasoning: a
    # proposal nobody is there to raise, and a stale one nobody is there to
    # withdraw, are both control failures rather than rendering ones.
    start_position_loop(build_exit_assessment)
    # WAIT watches are the same class of control: a trigger nobody re-checks is
    # a missed entry; converting them must still go through Risk + desk publish,
    # never the broker.
    start_entry_watch_loop()
    yield
    await stop_log_retention(retention_stop, retention_task)
    # Before the scanner, so open SSE streams stop waiting on a server that is
    # already on its way out.
    DESK_BUS.close()
    stop_entry_watch_loop()
    stop_reconcile_loop()
    stop_position_loop()
    stop_scanner()


# Interactive docs enumerate every route including the decision endpoints, so
# they are a development convenience only.
_DOCS = is_development()

app = FastAPI(
    title=settings.app_name,
    version="0.7.0",
    description="Traido — fill-aware confirmation desk; agents propose, you confirm.",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)

app.add_middleware(ApiAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)

app.include_router(desk_router)
app.include_router(logs_router)
app.include_router(review_router)
app.include_router(scan_router)
app.include_router(trading_router)
app.include_router(evaluation_router)
app.include_router(strategies_router)


@app.get("/")
async def root() -> RedirectResponse:
    """UI lives on the Vite React desk — redirect away from the API root."""
    return RedirectResponse(url=DASHBOARD_URL, status_code=307)


@app.get("/health")
async def health() -> dict:
    """Liveness only — deliberately touches no dependency."""
    kill = get_kill_switch_state()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": app.version,
        "stage": 7,
        "environment": settings.environment,
        "broker_env": settings.broker_env.value,
        "trading_mode": settings.trading_mode.value,
        "mode": "confirmation_desk",
        "ui": "vite_react",
        "live_trading": False,
        "kill_switch": kill.enabled,
        "kill_switch_source": kill.source,
        "auth": auth_mode(),
        "dashboard": DASHBOARD_URL,
        "readiness": "/health/ready",
        "desk": "/api/v1/desk",
        "review": "/api/v1/review",
        "positions": "/api/v1/positions",
        "evaluation": "/api/v1/evaluation/{symbol}",
        "scanner_run": "/api/v1/scanner/run",
        "metrics": "/metrics",
        "alpaca_configured": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
    }


@app.get("/health/ready")
async def readiness() -> JSONResponse:
    """Readiness — checks dependencies and reports each one separately."""
    report = await build_readiness(settings)
    return JSONResponse(report.as_dict(), status_code=200 if report.ready else 503)


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus text exposition of the in-process registry.

    Not authenticated for the same reason `/health` is not: it carries counts,
    never a symbol, an account value or a credential. Cardinality is bounded at
    the registry, so this endpoint cannot grow with the size of the universe.
    """
    from core.metrics import METRICS

    return Response(
        content=METRICS.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
