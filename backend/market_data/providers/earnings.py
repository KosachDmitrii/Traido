"""
Earnings calendar (Finnhub).

Holding a swing position through an earnings print is a coin flip: the stop is
irrelevant because the gap opens past it. The Risk Engine can refuse those
trades, but only if something tells it when the print is. That is this module.

With no API key, or on any vendor error, it reports "unknown" rather than "no
earnings soon", and says which of the two it is. Inventing a clean calendar
would be worse than admitting we do not have one. What happens to a trade whose
calendar is unknown is the Risk Engine's decision, not this module's — see
`RiskLimits.require_earnings_check`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from core.clock import market_date
from core.enums import EarningsCheck
from core.vendor_http import describe_http_error, get_with_retry

FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/earnings"
CACHE_TTL = timedelta(hours=6)
# A failed read is cached far more briefly than a successful one, and the two
# must never share a TTL. An earnings date is stable for hours, which is what
# makes the long TTL right; "Finnhub did not answer" is stable for seconds, and
# caching it for six hours blackballs the symbol for the rest of the session
# over one 503 — a refused entry every cycle long after the vendor recovered.
# Short, but not zero: it keeps a burst of lookups for one symbol from becoming
# a burst of retries against a vendor already in trouble.
FAILURE_TTL = timedelta(minutes=2)
LOOKBACK_DAYS = 14
LOOKAHEAD_DAYS = 90
REQUEST_TIMEOUT = 8.0


@dataclass(frozen=True)
class EarningsInfo:
    symbol: str
    next_date: date | None = None
    last_date: date | None = None
    status: EarningsCheck = EarningsCheck.NOT_CHECKED
    note: str = ""

    @property
    def available(self) -> bool:
        """The calendar answered. Says nothing about whether a print is near."""
        return self.status is EarningsCheck.CHECKED

    def days_until_next(self, today: date | None = None) -> int | None:
        if self.next_date is None:
            return None
        return (self.next_date - (today or market_date())).days


@dataclass
class _CacheEntry:
    info: EarningsInfo
    fetched_at: datetime


class EarningsCalendar:
    """Cached Finnhub earnings lookups. Safe to share across the process."""

    def __init__(
        self,
        api_key: str | None,
        *,
        ttl: timedelta = CACHE_TTL,
        failure_ttl: timedelta = FAILURE_TTL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._transport = transport
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._said_unconfigured = False

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _cached(self, symbol: str, now: datetime) -> EarningsInfo | None:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        ttl = self._ttl if entry.info.available else self._failure_ttl
        if now - entry.fetched_at > ttl:
            return None
        return entry.info

    async def get(self, symbol: str, *, now: datetime | None = None) -> EarningsInfo:
        symbol = symbol.upper()
        now = now or datetime.now(UTC)

        cached = self._cached(symbol, now)
        if cached is not None:
            return cached

        if not self._api_key:
            # A missing key is one condition affecting the whole universe, not a
            # discovery about this symbol. The prose is worth saying once; said
            # per symbol it is sixty identical warnings a cycle burying the notes
            # that are genuinely per-symbol. The status is returned every time —
            # that is the part the risk engine reads.
            first = not self._said_unconfigured
            self._said_unconfigured = True
            return EarningsInfo(
                symbol=symbol,
                status=EarningsCheck.NOT_CONFIGURED,
                note="Finnhub key not configured — earnings risk unchecked" if first else "",
            )

        async with self._lock:
            cached = self._cached(symbol, now)
            if cached is not None:
                return cached
            info = await self._fetch(symbol, now)
            self._cache[symbol] = _CacheEntry(info=info, fetched_at=now)
            return info

    async def _fetch(self, symbol: str, now: datetime) -> EarningsInfo:
        # Finnhub's dates are exchange dates, and `today` both bounds the query
        # and splits the answer into next and last. Taken in UTC it would move
        # a print from "next" to "last" during the evening it is announced.
        today = market_date(now)
        params = {
            "symbol": symbol,
            "from": (today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
            "to": (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
        }
        # Header, not `token=`: a key in the URL reaches every log that renders
        # a request or an exception. See `core.redaction`.
        headers = {"X-Finnhub-Token": self._api_key or ""}
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=self._transport
            ) as client:
                response = await get_with_retry(
                    client, FINNHUB_CALENDAR_URL, params=params, headers=headers
                )
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return EarningsInfo(
                symbol=symbol,
                status=EarningsCheck.UNAVAILABLE,
                note=f"Earnings lookup failed: {describe_http_error(exc)}",
            )

        return parse_earnings_payload(symbol, payload, today)


def parse_earnings_payload(symbol: str, payload: object, today: date) -> EarningsInfo:
    """
    Split a Finnhub calendar response into the next and most recent print.

    Pure function so the parsing rules are testable without a network call.
    """
    rows = payload.get("earningsCalendar") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return EarningsInfo(
            symbol=symbol,
            status=EarningsCheck.UNAVAILABLE,
            note="Unexpected calendar payload",
        )

    dates: list[date] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("date")
        if not isinstance(raw, str):
            continue
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            continue

    if not dates:
        return EarningsInfo(
            symbol=symbol,
            status=EarningsCheck.CHECKED,
            note="No earnings scheduled in window",
        )

    dates.sort()
    future = [d for d in dates if d >= today]
    past = [d for d in dates if d < today]

    return EarningsInfo(
        symbol=symbol,
        next_date=future[0] if future else None,
        last_date=past[-1] if past else None,
        status=EarningsCheck.CHECKED,
        note="",
    )


_CALENDAR: EarningsCalendar | None = None


def get_earnings_calendar(api_key: str | None) -> EarningsCalendar:
    """Process-wide calendar so the cache is shared across scan cycles."""
    global _CALENDAR
    if _CALENDAR is None or _CALENDAR._api_key != api_key:
        _CALENDAR = EarningsCalendar(api_key)
    return _CALENDAR
