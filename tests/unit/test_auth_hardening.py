"""
API auth invariants.

The desk can flatten positions and confirm entries over HTTP, so an
unauthenticated `/api/*` is a capital risk, not just an infosec one. These
tests pin the behaviour that keeps a misconfigured deploy loud instead of open.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from api.auth import (
    ApiAuthMiddleware,
    _is_public,
    assert_auth_configured,
    auth_bypass_allowed,
    auth_mode,
)


def _request(path: str, *, host: str | None, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": (host, 12345) if host else None,
    }
    return Request(scope)


async def _call(request: Request):  # type: ignore[no-untyped-def]
    async def _next(_req):  # type: ignore[no-untyped-def]
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    middleware = ApiAuthMiddleware(app=None)  # type: ignore[arg-type]
    return await middleware.dispatch(request, _next)


# ── Startup guard ────────────────────────────────────────────────────────────


def test_production_without_an_api_key_refuses_to_boot(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "production")
    monkeypatch.delenv("TRAIDO_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="TRAIDO_API_KEY"):
        assert_auth_configured()


def test_production_with_an_api_key_boots(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "production")
    monkeypatch.setenv("TRAIDO_API_KEY", "s3cret")

    assert_auth_configured()


def test_development_may_boot_without_a_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "development")
    monkeypatch.delenv("TRAIDO_API_KEY", raising=False)

    assert_auth_configured()


# ── The disable flag must not reach production ───────────────────────────────


def test_auth_disabled_is_ignored_outside_development(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A stray env var must not be able to strip auth off a deployed desk."""
    monkeypatch.setenv("TRAIDO_ENV", "production")
    monkeypatch.setenv("TRAIDO_AUTH_DISABLED", "1")

    assert auth_bypass_allowed() is False


def test_auth_disabled_is_honoured_in_tests(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "test")
    monkeypatch.setenv("TRAIDO_AUTH_DISABLED", "1")

    assert auth_bypass_allowed() is True
    assert auth_mode() == "disabled"


# ── Key enforcement ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_wrong_key_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "production")
    monkeypatch.setenv("TRAIDO_API_KEY", "right")
    monkeypatch.delenv("TRAIDO_AUTH_DISABLED", raising=False)

    resp = await _call(_request("/api/v1/desk", host="1.2.3.4", headers={"X-API-Key": "wrong"}))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_localhost_does_not_bypass_a_configured_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Once a key exists it applies to everyone, including loopback."""
    monkeypatch.setenv("TRAIDO_ENV", "production")
    monkeypatch.setenv("TRAIDO_API_KEY", "right")
    monkeypatch.delenv("TRAIDO_AUTH_DISABLED", raising=False)

    resp = await _call(_request("/api/v1/desk", host="127.0.0.1"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_the_correct_key_passes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "production")
    monkeypatch.setenv("TRAIDO_API_KEY", "right")
    monkeypatch.delenv("TRAIDO_AUTH_DISABLED", raising=False)

    resp = await _call(_request("/api/v1/desk", host="1.2.3.4", headers={"X-API-Key": "right"}))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_keyless_mode_refuses_remote_callers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRAIDO_ENV", "development")
    monkeypatch.delenv("TRAIDO_API_KEY", raising=False)
    monkeypatch.delenv("TRAIDO_AUTH_DISABLED", raising=False)

    resp = await _call(_request("/api/v1/desk", host="203.0.113.9"))
    assert resp.status_code == 403


# ── Public surface ───────────────────────────────────────────────────────────


def test_health_is_public_but_lookalike_paths_are_not() -> None:
    assert _is_public("/health") is True
    assert _is_public("/health/ready") is True
    assert _is_public("/health-internal") is False
    assert _is_public("/api/v1/desk") is False
