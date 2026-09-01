"""Company display names from Finnhub profile2 — display only, fail soft."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from market_data.providers.company_name import (
    CompanyNameResolver,
    parse_name_payload,
)

_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
_KEY = "k" * 20


def test_parse_name_from_profile() -> None:
    info = parse_name_payload("KO", {"name": "  Coca-Cola   Company ", "ticker": "KO"})
    assert info.ok
    assert info.name == "Coca-Cola Company"


def test_empty_profile_has_no_name() -> None:
    info = parse_name_payload("ZZZZ", {})
    assert not info.ok
    assert info.name is None


def test_blank_name_is_absent() -> None:
    info = parse_name_payload("X", {"name": "   "})
    assert not info.ok
    assert info.name is None


@pytest.mark.asyncio
async def test_resolve_caches_success() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"name": "Coca-Cola Company", "ticker": "KO"})

    resolver = CompanyNameResolver(_KEY, transport=httpx.MockTransport(handler))
    first = await resolver.resolve("ko", now=_NOW)
    second = await resolver.resolve("KO", now=_NOW + timedelta(hours=1))
    assert first.name == "Coca-Cola Company"
    assert second.name == "Coca-Cola Company"
    assert calls == 1
    assert resolver.peek("KO", now=_NOW) == "Coca-Cola Company"


@pytest.mark.asyncio
async def test_unconfigured_returns_none() -> None:
    resolver = CompanyNameResolver(None)
    info = await resolver.resolve("KO", now=_NOW)
    assert info.name is None
    assert not info.ok


@pytest.mark.asyncio
async def test_resolve_many_maps_symbols() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sym = request.url.params.get("symbol", "")
        return httpx.Response(200, json={"name": f"{sym} Corp", "ticker": sym})

    resolver = CompanyNameResolver(_KEY, transport=httpx.MockTransport(handler))
    names = await resolver.resolve_many(["aapl", "MSFT", "aapl"])
    assert names == {"AAPL": "AAPL Corp", "MSFT": "MSFT Corp"}
