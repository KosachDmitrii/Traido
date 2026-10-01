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
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from core.concurrency import RateLimiter
from core.enums import SectorCheck
from core.universe import ETF_SECTOR, UNKNOWN_SECTOR, Universe, default_universe
from core.vendor_http import describe_http_error, get_with_retry

FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
# A sector rarely moves; days is the right unit. A failed read must not share
# that TTL — see earnings.py for the same split.
CACHE_TTL = timedelta(days=7)
FAILURE_TTL = timedelta(minutes=2)
REQUEST_TIMEOUT = 8.0
logger = logging.getLogger(__name__)
CLASSIFICATION_REVISION = "sector_resolver@4"

# profile2 returns an industry, which need not be one of the eleven sector names.
# Exact industry names follow the GICS sector/industry structure:
# https://www.msci.com/indexes/documents/methodology/0_MSCI_Global_Industry_Classification_Standard_GICS_Methodology_20240801.pdf
# Finnhub is NOT GICS: coarse/conflicting labels (Electrical Equipment, Retail,
# REITs, Conglomerates) remain unknown unless a verified symbol override exists.
# Ambiguous/unknown labels remain unclassified; never use substring matching.
INDUSTRY_TO_SECTOR: dict[str, str] = {
    "technology": "technology",
    "information technology": "technology",
    "software": "technology",
    "it services": "technology",
    "software & it services": "technology",
    "communications equipment": "technology",
    "technology hardware, storage & peripherals": "technology",
    "electronic equipment, instruments & components": "technology",
    "semiconductors & semiconductor equipment": "technology",
    "semiconductors": "technology",
    "communication services": "communication",
    "diversified telecommunication services": "communication",
    "wireless telecommunication services": "communication",
    "media": "communication",
    "entertainment": "communication",
    "interactive media & services": "communication",
    "consumer cyclical": "consumer_discretionary",
    "consumer discretionary": "consumer_discretionary",
    "automobile components": "consumer_discretionary",
    "automobiles": "consumer_discretionary",
    "household durables": "consumer_discretionary",
    "leisure products": "consumer_discretionary",
    "textiles, apparel & luxury goods": "consumer_discretionary",
    "hotels, restaurants & leisure": "consumer_discretionary",
    "specialty retail": "consumer_discretionary",
    "broadline retail": "consumer_discretionary",
    "consumer defensive": "consumer_staples",
    "consumer staples": "consumer_staples",
    "consumer staples distribution & retail": "consumer_staples",
    "food products": "consumer_staples",
    "beverages": "consumer_staples",
    "tobacco": "consumer_staples",
    "household products": "consumer_staples",
    "personal care products": "consumer_staples",
    "financial services": "financials",
    "financials": "financials",
    "banks": "financials",
    "banking": "financials",
    "capital markets": "financials",
    "consumer finance": "financials",
    "insurance": "financials",
    "mortgage real estate investment trusts (reits)": "financials",
    "healthcare": "healthcare",
    "health care": "healthcare",
    "biotechnology": "healthcare",
    "pharmaceuticals": "healthcare",
    "health care equipment & supplies": "healthcare",
    "health care providers & services": "healthcare",
    "health care technology": "healthcare",
    "life sciences tools & services": "healthcare",
    "energy": "energy",
    "energy equipment & services": "energy",
    "oil, gas & consumable fuels": "energy",
    "industrials": "industrials",
    "aerospace & defense": "industrials",
    "building products": "industrials",
    "construction & engineering": "industrials",
    "construction": "industrials",
    "industrial conglomerates": "industrials",
    "machinery": "industrials",
    "commercial services & supplies": "industrials",
    "professional services": "industrials",
    "air freight & logistics": "industrials",
    "passenger airlines": "industrials",
    "airlines": "industrials",
    "marine transportation": "industrials",
    "ground transportation": "industrials",
    "road & rail": "industrials",
    "transportation infrastructure": "industrials",
    "basic materials": "materials",
    "materials": "materials",
    "chemicals": "materials",
    "construction materials": "materials",
    "containers & packaging": "materials",
    "packaging": "materials",
    "metals & mining": "materials",
    "paper & forest products": "materials",
    "utilities": "utilities",
    "electric utilities": "utilities",
    "gas utilities": "utilities",
    "multi-utilities": "utilities",
    "water utilities": "utilities",
    "independent power and renewable electricity producers": "utilities",
    "real estate": "real_estate",
    "equity real estate investment trusts (reits)": "real_estate",
    "real estate management & development": "real_estate",
    "industrial reits": "real_estate",
    "hotel & resort reits": "real_estate",
    "office reits": "real_estate",
    "health care reits": "real_estate",
    "residential reits": "real_estate",
    "retail reits": "real_estate",
    "specialized reits": "real_estate",
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
    industry: str | None = None

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
            industry=industry,
        )
    return SectorInfo(
        symbol=symbol,
        sector=sector,
        status=SectorCheck.CHECKED,
        source="finnhub",
        industry=industry,
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
        persistent: bool = False,
    ) -> None:
        self._api_key = api_key
        self._universe = universe if universe is not None else default_universe()
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._transport = transport
        self._persistent = persistent
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # At most 20 profile attempts/minute, including retries. Leave room
        # in the free vendor account for foreground news and earnings reads.
        self._limiter = RateLimiter(1 / 3, burst=1)
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
        if not timedelta(0) <= now - entry.fetched_at <= ttl:
            return None
        return entry.info

    async def resolve(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        asset_class: str | None = None,
    ) -> SectorInfo:
        symbol = symbol.upper()
        # A caller may pass the earlier quote-evaluation time. Classification
        # fetched after that instant is not "from the future" at lookup time.
        # Production freshness uses the actual lookup clock; mock transports
        # may supply a deterministic clock for TTL/restart regression tests.
        now = (now or datetime.now(UTC)) if self._transport is not None else datetime.now(UTC)

        # Alpaca identifies ETFs in its reference feed. Funds do not have a
        # corporate industry for Finnhub to classify, so retain that explicit
        # identity instead of turning a blank company profile into missing
        # metadata. A separate bars-based gate still decides tradability.
        if str(getattr(asset_class, "value", asset_class) or "").lower() == "etf":
            return SectorInfo(
                symbol=symbol,
                sector=ETF_SECTOR,
                status=SectorCheck.CHECKED,
                source="alpaca_asset",
            )

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

        async with self._locks.setdefault(symbol, asyncio.Lock()):
            curated = self._from_universe(symbol)
            if curated is not None:
                return curated
            cached = self._cached(symbol, now)
            if cached is not None:
                return cached
            if self._persistent:
                from market_data.providers import sector_store

                raw = await asyncio.to_thread(sector_store.read, symbol)
                entry = self._restore(symbol, raw)
                if entry is not None:
                    self._cache[symbol] = entry
                    cached = self._cached(symbol, now)
                    if cached is not None:
                        return cached
            info = await self._fetch(symbol)
            # Production uses completion time, not an earlier request start.
            fetched_at = datetime.now(UTC) if self._transport is None else now
            if self._persistent:
                await asyncio.to_thread(
                    sector_store.write,
                    symbol,
                    {
                        "version": CLASSIFICATION_REVISION,
                        "fetched_at": fetched_at.isoformat(),
                        "sector": info.sector,
                        "status": info.status.value,
                        "source": info.source,
                        "note": info.note,
                        "industry": info.industry,
                    },
                )
            self._cache[symbol] = _CacheEntry(info=info, fetched_at=fetched_at)
            if not info.available:
                logger.warning(
                    "Sector classification unavailable: symbol=%s status=%s source=%s detail=%s",
                    symbol,
                    info.status.value,
                    info.source,
                    info.note,
                )
            else:
                logger.info(
                    "Sector classification resolved: symbol=%s sector=%s industry=%s version=%s",
                    symbol,
                    info.sector,
                    info.industry,
                    CLASSIFICATION_REVISION,
                )
            return info

    @staticmethod
    def _restore(symbol: str, raw: object) -> _CacheEntry | None:
        if not isinstance(raw, dict) or raw.get("version") not in {
            CLASSIFICATION_REVISION,
            "sector_resolver@3",
        }:
            return None
        try:
            fetched_at = datetime.fromisoformat(raw["fetched_at"])
            status = SectorCheck(raw["status"])
            # Preserve already-warmed v3 success only if its original industry
            # reproduces the same sector under v4 below. Older failures must be
            # retried, since the new aliases may resolve them now.
            if raw["version"] != CLASSIFICATION_REVISION and status is not SectorCheck.CHECKED:
                return None
            sector = raw.get("sector")
            if fetched_at.tzinfo is None or raw.get("source") != "finnhub":
                return None
            if status is SectorCheck.CHECKED and (
                not isinstance(raw.get("industry"), str)
                or map_finnhub_industry(raw["industry"]) != sector
                or sector not in KNOWN_SECTORS
            ):
                return None
            if status is not SectorCheck.CHECKED and sector is not None:
                return None
            return _CacheEntry(
                info=SectorInfo(
                    symbol=symbol,
                    sector=sector,
                    status=status,
                    source="finnhub",
                    note=raw.get("note", ""),
                    industry=raw.get("industry"),
                ),
                fetched_at=fetched_at,
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _fetch(self, symbol: str) -> SectorInfo:
        params = {"symbol": symbol}
        headers = {"X-Finnhub-Token": self._api_key or ""}
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=self._transport
            ) as client:
                response = await get_with_retry(
                    client,
                    FINNHUB_PROFILE_URL,
                    params=params,
                    headers=headers,
                    before_attempt=self._limiter.acquire,
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
        _RESOLVER = SectorResolver(api_key, persistent=True)
    return _RESOLVER
