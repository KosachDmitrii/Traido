"""Explicit company sectors recover ambiguous labels without guessing industries."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.enums import SectorCheck
from core.universe import Universe
from market_data.providers import sector_store
from market_data.providers.nasdaq_sector import (
    STORE_KEY,
    TTL,
    NasdaqSectorDirectory,
    parse_rows,
)
from market_data.providers.sector import SectorResolver

NOW = datetime(2026, 10, 5, 14, tzinfo=UTC)


def payload(*rows):
    return {"data": {"rows": list(rows), "totalrecords": len(rows)}}


def row(symbol, sector, industry="Electrical Products"):
    return {"symbol": symbol, "sector": sector, "industry": industry}


@pytest.mark.asyncio
async def test_ambiguous_finnhub_labels_use_exact_company_sectors_and_one_bulk_read():
    requests = []

    def handle(request):
        requests.append(request.url.host)
        if request.url.host == "finnhub.io":
            return httpx.Response(200, json={"finnhubIndustry": "Electrical Equipment"})
        return httpx.Response(
            200, json=payload(row("FLEX", "Technology"), row("TESTIND", "Industrials"))
        )

    transport = httpx.MockTransport(handle)
    directory = NasdaqSectorDirectory(transport=transport)
    resolver = SectorResolver(
        "key", universe=Universe(symbols=[], sectors={}), transport=transport, directory=directory
    )
    from core.concurrency import RateLimiter

    resolver._limiter = RateLimiter(1e6, burst=1e6)
    results = await asyncio.gather(
        resolver.resolve("FLEX", now=NOW), resolver.resolve("TESTIND", now=NOW)
    )
    assert [info.sector for info in results] == ["technology", "industrials"]
    assert all(info.status is SectorCheck.CHECKED and info.source == "nasdaq" for info in results)
    assert requests.count("api.nasdaq.com") == 1
    assert results[0].source_ts == NOW
    assert results[0].source_sector == "Technology"


@pytest.mark.asyncio
async def test_restart_restores_bulk_and_symbol_evidence_without_refreshing_age():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=payload(row("FLEX", "Technology")))
    )
    directory = NasdaqSectorDirectory(transport=transport, persistent=True)
    first = SectorResolver(
        None,
        universe=Universe(symbols=[], sectors={}),
        transport=transport,
        persistent=True,
        directory=directory,
    )
    info = await first.resolve("FLEX", now=NOW)
    assert info.available
    assert sector_store.read("FLEX")["fetched_at"] == NOW.isoformat()
    assert STORE_KEY not in sector_store.observations()

    def no_request(request):
        pytest.fail("Fresh durable evidence must not issue vendor reads")

    restarted_directory = NasdaqSectorDirectory(
        transport=httpx.MockTransport(no_request), persistent=True
    )
    restored = await restarted_directory.lookup("FLEX", now=NOW + timedelta(days=1))
    assert restored.sector == "technology"
    assert restored.fetched_at == NOW
    restored_info = SectorResolver._restore("FLEX", sector_store.read("FLEX"))
    assert restored_info.info.source == "nasdaq"
    assert restored_info.info.source_ts == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "future", "naive", "symbol", "sector", "version"])
async def test_invalid_directory_is_refetched_and_never_authorizes_stale_data(change):
    directory = NasdaqSectorDirectory(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=payload(row("FLEX", "Technology")))
        ),
        persistent=True,
    )
    await directory.lookup("FLEX", now=NOW)
    raw = sector_store.read(STORE_KEY)
    if change == "expired":
        raw["fetched_at"] = (NOW - TTL - timedelta(seconds=1)).isoformat()
    elif change == "future":
        raw["fetched_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "naive":
        raw["fetched_at"] = NOW.replace(tzinfo=None).isoformat()
    elif change == "symbol":
        raw["rows"]["FLEX"]["symbol"] = "ETN"
    elif change == "sector":
        raw["rows"]["FLEX"]["source_sector"] = "Other"
    else:
        raw["version"] = "bad"
    sector_store.write(STORE_KEY, raw)
    calls = []

    def fail(request):
        calls.append(request.url)
        return httpx.Response(403)

    restarted = NasdaqSectorDirectory(transport=httpx.MockTransport(fail), persistent=True)
    assert await restarted.lookup("FLEX", now=NOW) is None
    assert await restarted.lookup("ETN", now=NOW) is None
    assert len(calls) == 1  # A provider failure does not create one request per symbol.


@pytest.mark.asyncio
async def test_expired_in_memory_directory_is_not_reused_after_refresh_failure():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, json=payload(row("FLEX", "Technology")))
            if calls == 1
            else httpx.Response(403)
        )

    directory = NasdaqSectorDirectory(transport=httpx.MockTransport(handle))
    assert await directory.lookup("FLEX", now=NOW)
    assert await directory.lookup("FLEX", now=NOW + TTL + timedelta(seconds=1)) is None


@pytest.mark.asyncio
async def test_absent_symbol_does_not_borrow_another_company_sector():
    directory = NasdaqSectorDirectory(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=payload(row("FLEX", "Technology")))
        )
    )
    assert await directory.lookup("NOTFLEX", now=NOW) is None


@pytest.mark.parametrize(
    "sector",
    [
        "Technology",
        "Telecommunications",
        "Consumer Discretionary",
        "Consumer Staples",
        "Finance",
        "Health Care",
        "Energy",
        "Industrials",
        "Basic Materials",
        "Utilities",
        "Real Estate",
    ],
)
def test_all_sector_groups_accept_explicit_labels(sector):
    assert "TEST" in parse_rows(payload(row("TEST", sector)))


def test_conflicting_duplicate_symbols_and_unknown_sectors_are_not_classified():
    rows = parse_rows(
        payload(
            row("FLEX", "Technology"),
            row("FLEX", "Industrials"),
            row("TEST", "Energy"),
            row("BAD", "Other"),
        )
    )
    assert set(rows) == {"TEST"}


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"data": {"rows": []}},
        {"data": {"rows": [row("FLEX", "Technology")], "totalrecords": 2}},
    ],
)
def test_bad_or_truncated_catalogue_is_refused(bad):
    with pytest.raises((TypeError, ValueError)):
        parse_rows(bad)


@pytest.mark.asyncio
async def test_both_sources_missing_remain_fail_closed():
    directory = NasdaqSectorDirectory(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    resolver = SectorResolver(
        "key",
        universe=Universe(symbols=[], sectors={}),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"finnhubIndustry": "Electrical Equipment"})
        ),
        directory=directory,
    )
    info = await resolver.resolve("FLEX", now=NOW)
    assert info.status is SectorCheck.UNCLASSIFIED
    assert info.sector is None


@pytest.mark.asyncio
async def test_fallback_flows_to_canonical_benchmark_and_risk_context(monkeypatch):
    from unittest.mock import AsyncMock

    from core.enums import NewsCheck
    from market_data.providers import sector
    from risk import context_builder
    from risk.context_builder import build_risk_context
    from trading.sector_classification import resolve_symbol_classification

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=payload(row("FLEX", "Technology")))
    )
    directory = NasdaqSectorDirectory(transport=transport)
    shared = SectorResolver(
        None, universe=Universe(symbols=[], sectors={}), transport=transport, directory=directory
    )
    monkeypatch.setattr(sector, "get_sector_resolver", lambda key: shared)
    monkeypatch.setattr(context_builder, "get_sector_resolver", lambda key: shared)
    classification = await resolve_symbol_classification("FLEX", finnhub_api_key=None, now=NOW)
    assert classification.sector == "technology"
    assert classification.benchmark == "XLK"
    assert classification.classification_provider == "nasdaq"
    broker = AsyncMock()
    broker.list_positions.return_value = []
    built = await build_risk_context(
        "FLEX", broker=broker, market_data=None, news=NewsCheck.CHECKED, now=NOW
    )
    assert built.context.sector == "technology"
    assert built.context.sector_check is SectorCheck.CHECKED


@pytest.mark.asyncio
async def test_later_symbol_lookup_keeps_directory_source_time_in_durable_cache():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json=payload(row("FLEX", "Technology"), row("TEST", "Energy"))
        )
    )
    directory = NasdaqSectorDirectory(transport=transport, persistent=True)
    shared = SectorResolver(
        None,
        universe=Universe(symbols=[], sectors={}),
        transport=transport,
        persistent=True,
        directory=directory,
    )
    await shared.resolve("FLEX", now=NOW)
    info = await shared.resolve("TEST", now=NOW + timedelta(days=6))
    assert info.source_ts == NOW
    assert sector_store.read("TEST")["fetched_at"] == NOW.isoformat()
    raw = sector_store.read("TEST")
    raw["symbol"] = "FLEX"
    assert SectorResolver._restore("TEST", raw) is None
