"""
Sector classification (curated map + Finnhub profile2).

`configs/universe.json` is the operator's word and always wins. Names outside
that file are asked of Finnhub `/stock/profile2`; the industry string is mapped
onto the same eleven groups the file uses. An empty profile, an unmapped
industry, a missing key, or a vendor outage is reported as such — never as a
guessed sector. Inventing a bucket is how a name used to skip its real cap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from core.enums import SectorCheck
from core.universe import UNKNOWN_SECTOR, Universe, default_universe
from core.vendor_http import describe_http_error, get_with_retry

FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
# A sector rarely moves; days is the right unit. A failed read must not share
# that TTL — see earnings.py for the same split.
CACHE_TTL = timedelta(days=7)
FAILURE_TTL = timedelta(minutes=2)
REQUEST_TIMEOUT = 8.0

# Finnhub's `finnhubIndustry` is its own taxonomy, roughly GICS top-level.
# Anything not listed here stays unclassified: silence beats a wrong bucket.
INDUSTRY_TO_SECTOR: dict[str, str] = {
    "technology": "technology",
    "communication services": "communication",
    "consumer cyclical": "consumer_discretionary",
    "consumer defensive": "consumer_staples",
    "financial services": "financials",
    "healthcare": "healthcare",
    "energy": "energy",
    "industrials": "industrials",
    "basic materials": "materials",
    "utilities": "utilities",
    "real estate": "real_estate",
}

# Our eleven groups plus the ETF bucket from the curated file.
KNOWN_SECTORS = frozenset(INDUSTRY_TO_SECTOR.values()) | {"etf"}


@dataclass(frozen=True)
class SectorInfo:
    symbol: str
    sector: str | None = None
    status: SectorCheck = SectorCheck.NOT_CHECKED
    source: str = ""
    note: str = ""

    @property
    def available(self) -> bool:
        return self.status is SectorCheck.CHECKED and self.sector is not None


@dataclass
class _CacheEntry:
    info: SectorInfo
    fetched_at: datetime


def map_finnhub_industry(industry: str | None) -> str | None:
    """Map a Finnhub industry string onto one of our eleven groups, or None."""
    if industry is None:
        return None
    key = " ".join(str(industry).strip().lower().split())
    if not key:
        return None
    return INDUSTRY_TO_SECTOR.get(key)


def parse_profile_payload(symbol: str, payload: object) -> SectorInfo:
    """Turn a profile2 body into a SectorInfo. Pure — no I/O."""
    if not isinstance(payload, dict) or not payload:
        # Finnhub answers `{}` for an unknown ticker. That is "we looked and
        # there is no industry", not "the vendor was down".
        return SectorInfo(
            symbol=symbol,
            status=SectorCheck.UNCLASSIFIED,
            source="finnhub",
            note="Finnhub profile empty — sector unclassified",
        )

    raw = payload.get("finnhubIndustry")
    industry = raw if isinstance(raw, str) else None
    sector = map_finnhub_industry(industry)
    if sector is None:
        label = industry.strip() if isinstance(industry, str) and industry.strip() else "(blank)"
        return SectorInfo(
            symbol=symbol,
            status=SectorCheck.UNCLASSIFIED,
            source="finnhub",
            note=f"Finnhub industry unmapped: {label}",
        )
    return SectorInfo(
        symbol=symbol,
        sector=sector,
        status=SectorCheck.CHECKED,
        source="finnhub",
    )


class SectorResolver:
    """Curated map first, Finnhub for the rest. Safe to share across the process."""

    def __init__(
        self,
        api_key: str | None,
        *,
        universe: Universe | None = None,
        ttl: timedelta = CACHE_TTL,
        failure_ttl: timedelta = FAILURE_TTL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._universe = universe if universe is not None else default_universe()
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._transport = transport
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._said_unconfigured = False

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _from_universe(self, symbol: str) -> SectorInfo | None:
        curated = self._universe.sector_of(symbol)
        if curated == UNKNOWN_SECTOR:
            return None
        return SectorInfo(
            symbol=symbol,
            sector=curated,
            status=SectorCheck.CHECKED,
            source="universe",
        )

    def _cached(self, symbol: str, now: datetime) -> SectorInfo | None:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        ttl = self._ttl if entry.info.available else self._failure_ttl
        if now - entry.fetched_at > ttl:
            return None
        return entry.info

    async def resolve(self, symbol: str, *, now: datetime | None = None) -> SectorInfo:
        symbol = symbol.upper()
        now = now or datetime.now(UTC)

        curated = self._from_universe(symbol)
        if curated is not None:
            return curated

        cached = self._cached(symbol, now)
        if cached is not None:
            return cached

        if not self._api_key:
            first = not self._said_unconfigured
            self._said_unconfigured = True
            return SectorInfo(
                symbol=symbol,
                status=SectorCheck.NOT_CONFIGURED,
                note="Finnhub key not configured — sector unclassified" if first else "",
            )

        async with self._lock:
            curated = self._from_universe(symbol)
            if curated is not None:
                return curated
            cached = self._cached(symbol, now)
            if cached is not None:
                return cached
            info = await self._fetch(symbol)
            self._cache[symbol] = _CacheEntry(info=info, fetched_at=now)
            return info

    async def _fetch(self, symbol: str) -> SectorInfo:
        params = {"symbol": symbol}
        headers = {"X-Finnhub-Token": self._api_key or ""}
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=self._transport
            ) as client:
                response = await get_with_retry(
                    client, FINNHUB_PROFILE_URL, params=params, headers=headers
                )
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SectorInfo(
                symbol=symbol,
                status=SectorCheck.UNAVAILABLE,
                source="finnhub",
                note=f"Sector lookup failed: {describe_http_error(exc)}",
            )
        return parse_profile_payload(symbol, payload)


_RESOLVER: SectorResolver | None = None


def get_sector_resolver(api_key: str | None) -> SectorResolver:
    """Process-wide resolver so the multi-day cache is shared across cycles."""
    global _RESOLVER
    if _RESOLVER is None or _RESOLVER._api_key != api_key:
        _RESOLVER = SectorResolver(api_key)
    return _RESOLVER
