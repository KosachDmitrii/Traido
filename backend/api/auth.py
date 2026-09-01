"""
API auth.

Three modes, chosen by configuration rather than by guesswork:

- **API key** — `TRAIDO_API_KEY` is set. Every `/api/*` call must present a
  matching `X-API-Key`. This is the only mode fit for a non-localhost deploy.
- **Local only** — no key configured. `/api/*` is served to loopback clients
  and refused for everyone else. Convenient for development, useless as
  security, hence the startup guard below.
- **Disabled** — `TRAIDO_AUTH_DISABLED=1`. Honoured only when the environment
  is development or test. In any other environment the flag is ignored and the
  request is authenticated normally, because a stray env var must not be able
  to strip auth off a deployed desk.

`assert_auth_configured()` runs at startup and refuses to boot a non-development
environment that has no API key, so the failure is a loud crash on deploy
rather than a silently open endpoint.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient", "test"}
PUBLIC_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc")
DEV_ENVIRONMENTS = {"development", "dev", "local", "test", "testing"}
TRUTHY = {"1", "true", "yes", "on"}

QUERY_KEY_PATHS = {"/api/v1/desk/stream"}
"""
Endpoints that may authenticate with an `api_key` query parameter.

Restricted to the SSE stream because `EventSource` cannot send headers. Keys in
query strings end up in access logs, so this is an exception for one read-only
endpoint, never a general fallback.
"""


def _is_public(path: str) -> bool:
    # Prefixes match on a path boundary only, so a route like
    # "/health-internal" does not inherit "/health"'s public status.
    if path == "/":
        return True
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES)


def _client_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def current_environment() -> str:
    return (os.getenv("TRAIDO_ENV") or "development").strip().lower()


def is_development() -> bool:
    return current_environment() in DEV_ENVIRONMENTS


def auth_bypass_allowed() -> bool:
    """The disable flag is only honoured in development and test."""
    if (os.getenv("TRAIDO_AUTH_DISABLED") or "").lower() not in TRUTHY:
        return False
    if is_development():
        return True
    logger.error(
        "TRAIDO_AUTH_DISABLED is set but environment is %r — ignoring the flag",
        current_environment(),
    )
    return False


def assert_auth_configured() -> None:
    """
    Refuse to start an internet-reachable desk without an API key.

    Called from the app lifespan. Crashing at boot is the correct behaviour:
    a deployment that quietly falls back to loopback-only auth will appear
    healthy right up until it is reachable and unauthenticated.
    """
    if is_development():
        return
    if not (os.getenv("TRAIDO_API_KEY") or "").strip():
        raise RuntimeError(
            f"Refusing to start: TRAIDO_ENV={current_environment()!r} requires TRAIDO_API_KEY. "
            "Local-only auth is not a control outside development."
        )


def auth_mode() -> str:
    if auth_bypass_allowed():
        return "disabled"
    return "api_key" if (os.getenv("TRAIDO_API_KEY") or "").strip() else "local_only"


class ApiAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if _is_public(path) or not path.startswith("/api/"):
            return await call_next(request)

        if auth_bypass_allowed():
            return await call_next(request)

        api_key = (os.getenv("TRAIDO_API_KEY") or "").strip()
        if api_key:
            provided = request.headers.get("X-API-Key") or request.headers.get("x-api-key") or ""
            if not provided and path in QUERY_KEY_PATHS:
                provided = request.query_params.get("api_key") or ""
            if not hmac.compare_digest(provided, api_key):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

        # Local-only mode (no API key configured)
        host = _client_host(request)
        if host is None or host in LOCAL_HOSTS:
            return await call_next(request)
        return JSONResponse(
            {"detail": "local_only: set TRAIDO_API_KEY or call from localhost"},
            status_code=403,
        )
