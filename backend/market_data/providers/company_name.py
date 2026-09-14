"""
Company display names (Finnhub profile2).

The desk shows ticker + full name next to open positions. Sector resolution
already hits profile2 for names outside the curated universe, but curated
symbols never leave the file — so the name has to be its own lookup, with its
own cache. Display only: a missing name is a blank, never a refused trade.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from core.vendor_http import describe_http_error, get_with_retry

FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
CACHE_TTL = timedelta(days=7)
FAILURE_TTL = timedelta(minutes=2)
REQUEST_TIMEOUT = 8.0
PREFETCH_INTERVAL = 1.05


@dataclass(frozen=True)
class CompanyName:
    symbol: str
    name: str | None = None
    ok: bool = False
    note: str = ""


@dataclass
class _CacheEntry:
    info: CompanyName
    fetched_at: datetime


def parse_name_payload(symbol: str, payload: object) -> CompanyName:
    """Pull the display name out of a profile2 body. Pure — no I/O."""
    if not isinstance(payload, dict) or not payload:
        return CompanyName(symbol=symbol, note="Finnhub profile empty")
    raw = payload.get("name")
    if not isinstance(raw, str):
        return CompanyName(symbol=symbol, note="Finnhub profile has no name")
    name = " ".join(raw.split()).strip()
    if not name:
        return CompanyName(symbol=symbol, note="Finnhub profile has no name")
    return CompanyName(symbol=symbol, name=name, ok=True)


class CompanyNameResolver:
    """Cached Finnhub company names. Safe to share across the process."""

    def __init__(
        self,
        api_key: str | None,
        *,
        ttl: timedelta = CACHE_TTL,
        failure_ttl: timedelta = FAILURE_TTL,
        transport: httpx.AsyncBaseTransport | None = None,
        prefetch_interval: float = PREFETCH_INTERVAL,
    ) -> None:
        self._api_key = api_key
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._transport = transport
        self._prefetch_interval = prefetch_interval
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()
        self._prefetch_task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def peek(self, symbol: str, *, now: datetime | None = None) -> str | None:
        """Return a cached name without touching the network."""
        symbol = symbol.upper()
        now = now or datetime.now(UTC)
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        ttl = self._ttl if entry.info.ok else self._failure_ttl
        if now - entry.fetched_at > ttl:
            return None
        return entry.info.name

    def _cached(self, symbol: str, now: datetime) -> CompanyName | None:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        ttl = self._ttl if entry.info.ok else self._failure_ttl
        if now - entry.fetched_at > ttl:
            return None
        return entry.info

    async def resolve(self, symbol: str, *, now: datetime | None = None) -> CompanyName:
        symbol = symbol.upper()
        now = now or datetime.now(UTC)

        cached = self._cached(symbol, now)
        if cached is not None:
            return cached

        if not self._api_key:
            return CompanyName(symbol=symbol, note="Finnhub key not configured")

        async with self._lock:
            cached = self._cached(symbol, now)
            if cached is not None:
                return cached
            info = await self._fetch(symbol)
            self._cache[symbol] = _CacheEntry(info=info, fetched_at=now)
            return info

    async def resolve_many(self, symbols: list[str]) -> dict[str, str | None]:
        """Map SYMBOL → display name (or None). Failures stay None."""
        unique = list(dict.fromkeys(s.upper() for s in symbols if s))
        if not unique:
            return {}
        results = await asyncio.gather(*(self.resolve(s) for s in unique))
        return {info.symbol: info.name for info in results}

    def cached_many(self, symbols: list[str]) -> dict[str, str | None]:
        """Return only process-cached display data; never wait on Finnhub."""
        unique = list(dict.fromkeys(s.upper() for s in symbols if s))
        return {symbol: self.peek(symbol) for symbol in unique}

    def prefetch(self, symbols: list[str]) -> None:
        """Queue missing names behind one paced background worker.

        Company names are presentation data.  They must never hold up `/desk`,
        and starting hundreds of per-request tasks would merely move the same
        Finnhub rate-limit storm elsewhere.  One deduplicated worker warms the
        existing process cache gradually; later desk polls pick names up.
        """
        if not self.configured:
            return
        now = datetime.now(UTC)
        for raw in symbols:
            symbol = raw.upper()
            if not symbol or symbol in self._queued or self._cached(symbol, now) is not None:
                continue
            self._queued.add(symbol)
            self._pending.append(symbol)
        if self._pending and (self._prefetch_task is None or self._prefetch_task.done()):
            self._prefetch_task = asyncio.create_task(
                self._drain_prefetch(), name="company-name-prefetch"
            )

    async def _drain_prefetch(self) -> None:
        try:
            while self._pending:
                symbol = self._pending.popleft()
                self._queued.discard(symbol)
                await self.resolve(symbol)
                if self._pending and self._prefetch_interval > 0:
                    await asyncio.sleep(self._prefetch_interval)
        finally:
            self._prefetch_task = None

    async def _fetch(self, symbol: str) -> CompanyName:
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
            return CompanyName(
                symbol=symbol,
                note=f"Company name lookup failed: {describe_http_error(exc)}",
            )
        return parse_name_payload(symbol, payload)


_RESOLVER: CompanyNameResolver | None = None


def get_company_name_resolver(api_key: str | None) -> CompanyNameResolver:
    """Process-wide resolver so the multi-day cache is shared across polls."""
    global _RESOLVER
    if _RESOLVER is None or _RESOLVER._api_key != api_key:
        _RESOLVER = CompanyNameResolver(api_key)
    return _RESOLVER


async def attach_company_names(
    rows: list[dict],
    api_key: str | None,
    *,
    symbol_key: str = "symbol",
    name_key: str = "name",
) -> None:
    """Fill `name` on each row. Display only — never blocks trading."""
    if not rows:
        return
    resolver = get_company_name_resolver(api_key)
    names = await resolver.resolve_many([str(r.get(symbol_key) or "") for r in rows])
    for row in rows:
        sym = str(row.get(symbol_key) or "").upper()
        row[name_key] = names.get(sym)


def attach_cached_company_names(
    rows: list[dict],
    api_key: str | None,
    *,
    symbol_key: str = "symbol",
    name_key: str = "name",
) -> None:
    """Attach cached names immediately and warm misses outside the request."""
    if not rows:
        return
    resolver = get_company_name_resolver(api_key)
    symbols = [str(row.get(symbol_key) or "") for row in rows]
    names = resolver.cached_many(symbols)
    for row in rows:
        symbol = str(row.get(symbol_key) or "").upper()
        if not row.get(name_key):
            row[name_key] = names.get(symbol)
    resolver.prefetch(symbols)
