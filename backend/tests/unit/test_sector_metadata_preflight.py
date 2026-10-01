"""Durable classification cannot authorize stale or ambiguous sector evidence."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.concurrency import RateLimiter
from core.enums import SectorCheck
from market_data.providers import sector_store
from market_data.providers.sector import (
    CACHE_TTL,
    CLASSIFICATION_REVISION,
    FAILURE_TTL,
    SectorInfo,
    SectorResolver,
    map_finnhub_industry,
)

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


def resolver(payload, *, calls=None):
    def respond(request):
        if calls is not None:
            calls.append(request.url.params["symbol"])
        return httpx.Response(200, json=payload)

    value = SectorResolver("test-key", transport=httpx.MockTransport(respond), persistent=True)
    value._limiter = RateLimiter(1e6, burst=1e6)
    return value


@pytest.mark.parametrize(
    ("industry", "sector"),
    [
        ("Oil, Gas & Consumable Fuels", "energy"),
        ("Metals & Mining", "materials"),
        ("Aerospace & Defense", "industrials"),
        ("Airlines", "industrials"),
        ("Road & Rail", "industrials"),
        ("Construction", "industrials"),
        ("Packaging", "materials"),
        ("Specialty Retail", "consumer_discretionary"),
        ("Food Products", "consumer_staples"),
        ("Life Sciences Tools & Services", "healthcare"),
        ("Capital Markets", "financials"),
        ("Semiconductors", "technology"),
        ("Interactive Media & Services", "communication"),
        ("Electric Utilities", "utilities"),
        ("Health Care REITs", "real_estate"),
        ("Mortgage Real Estate Investment Trusts (REITs)", "financials"),
    ],
)
def test_exact_industries_cover_all_sectors(industry, sector):
    assert map_finnhub_industry(industry) == sector


@pytest.mark.parametrize("industry", ["Retail", "REITs", "Electrical Equipment", "Other", None])
def test_ambiguous_labels_remain_missing(industry):
    assert map_finnhub_industry(industry) is None


@pytest.mark.asyncio
async def test_restart_reuses_valid_observation_without_vendor_request():
    calls = []
    first = resolver({"finnhubIndustry": "Semiconductors"}, calls=calls)
    assert (await first.resolve("TESTCHIP", now=NOW)).available
    raw = sector_store.read("TESTCHIP")
    assert raw["version"] == CLASSIFICATION_REVISION
    assert raw["industry"] == "Semiconductors"
    assert raw["fetched_at"] == NOW.isoformat()
    restarted = resolver({}, calls=calls)
    restored = await restarted.resolve("TESTCHIP", now=NOW + timedelta(days=1))
    assert restored.sector == "technology"
    assert restored.source == "finnhub"
    assert calls == ["TESTCHIP"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "future", "naive", "old_version", "bad_sector"])
async def test_invalid_durable_success_cannot_pass(change):
    first = resolver({"finnhubIndustry": "Semiconductors"})
    await first.resolve("TESTCHIP", now=NOW)
    raw = sector_store.read("TESTCHIP")
    if change == "expired":
        raw["fetched_at"] = (NOW - CACHE_TTL - timedelta(seconds=1)).isoformat()
    elif change == "future":
        raw["fetched_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "naive":
        raw["fetched_at"] = NOW.replace(tzinfo=None).isoformat()
    elif change == "old_version":
        raw["version"] = "sector_resolver@2"
    else:
        raw["sector"] = "healthcare"
    sector_store.write("TESTCHIP", raw)
    calls = []
    rejected = await resolver({}, calls=calls).resolve("TESTCHIP", now=NOW)
    assert not rejected.available
    assert rejected.sector is None
    assert calls == ["TESTCHIP"]


@pytest.mark.asyncio
async def test_failure_ttl_survives_restart_and_recovers():
    await resolver({"finnhubIndustry": "Other"}).resolve("TESTCHIP", now=NOW)
    calls = []
    restarted = resolver({"finnhubIndustry": "Semiconductors"}, calls=calls)
    info = await restarted.resolve("TESTCHIP", now=NOW + timedelta(seconds=30))
    assert info.status is SectorCheck.UNCLASSIFIED
    assert info.industry == "Other"
    assert calls == []
    recovered = await restarted.resolve("TESTCHIP", now=NOW + FAILURE_TTL + timedelta(seconds=1))
    assert recovered.available
    assert calls == ["TESTCHIP"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_success", [False, True])
async def test_v3_upgrade_reuses_only_success_that_reproduces_under_current_map(prior_success):
    original = resolver({"finnhubIndustry": "Technology" if prior_success else "Airlines"})
    await original.resolve("TESTCHIP", now=NOW)
    raw = sector_store.read("TESTCHIP")
    raw["version"] = "sector_resolver@3"
    if not prior_success:
        raw.update(status=SectorCheck.UNCLASSIFIED.value, sector=None)
    sector_store.write("TESTCHIP", raw)
    calls = []
    current = await resolver({"finnhubIndustry": "Airlines"}, calls=calls).resolve(
        "TESTCHIP", now=NOW
    )
    assert current.available
    assert current.sector == ("technology" if prior_success else "industrials")
    assert calls == ([] if prior_success else ["TESTCHIP"])


@pytest.mark.asyncio
async def test_parallel_same_symbol_is_one_vendor_observation():
    calls = []
    shared = resolver({"finnhubIndustry": "Semiconductors"}, calls=calls)
    results = await asyncio.gather(*(shared.resolve("TESTCHIP", now=NOW) for _ in range(8)))
    assert all(info.sector == "technology" for info in results)
    assert calls == ["TESTCHIP"]
    assert set(sector_store.observations()) == {"TESTCHIP"}


@pytest.mark.asyncio
async def test_slow_background_symbol_does_not_lock_an_unrelated_entry():
    shared = resolver({})
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(symbol):
        if symbol == "TESTSLOW":
            started.set()
            await release.wait()
        return SectorInfo(
            symbol=symbol,
            sector="technology",
            status=SectorCheck.CHECKED,
            source="finnhub",
            industry="Semiconductors",
        )

    shared._fetch = fetch
    slow = asyncio.create_task(shared.resolve("TESTSLOW", now=NOW))
    try:
        await started.wait()
        fast = await asyncio.wait_for(shared.resolve("TESTFAST", now=NOW), timeout=2)
        assert fast.available
    finally:
        release.set()
        await slow


@pytest.mark.asyncio
async def test_production_cache_uses_lookup_clock_not_old_quote_time(monkeypatch):
    from market_data.providers import sector

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(sector, "datetime", Clock)
    shared = SectorResolver("test-key", persistent=True)
    calls = []

    async def fetch(symbol):
        calls.append(symbol)
        return SectorInfo(
            symbol=symbol,
            sector="technology",
            status=SectorCheck.CHECKED,
            source="finnhub",
            industry="Semiconductors",
        )

    shared._fetch = fetch
    assert (await shared.resolve("TESTCHIP", now=NOW - timedelta(seconds=10))).available
    assert (await shared.resolve("TESTCHIP", now=NOW - timedelta(seconds=10))).available
    assert calls == ["TESTCHIP"]


@pytest.mark.asyncio
async def test_preflight_does_not_block_scanner_and_prioritizes_ready(monkeypatch):
    from core.config import get_settings
    from market_data import sector_preflight

    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()
    sector_preflight._pending.clear()
    try:
        sector_preflight.offer(
            {
                "plans": {
                    "TESTWAIT": {"evidence": {}},
                    "TESTREADY": {"evidence": {"retest": {"target": "20"}}},
                    "AAPL": {"evidence": {}},
                    "TESTETF": {"evidence": {"instrument": {"asset_class": "etf"}}},
                }
            }
        )
        assert list(sector_preflight._pending) == ["TESTREADY", "TESTWAIT"]
    finally:
        sector_preflight._pending.clear()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_diagnostics_reports_unresolved_without_vendor_read():
    from api.routes.evaluation import sector_metadata_diagnostics

    now = datetime.now(UTC)
    await resolver({"finnhubIndustry": "Unmapped industry"}).resolve("TESTCHIP", now=now)
    report = await sector_metadata_diagnostics()
    assert report["counts"] == {"blocked": 1}
    assert report["unresolved"][0]["industry"] == "Unmapped industry"
