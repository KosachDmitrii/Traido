"""Alpaca Market Data adapter (OHLCV). Execution stays on BrokerPort."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from core.concurrency import RateLimiter
from core.config import get_settings
from core.enums import Timeframe
from core.schemas import Bar, Quote, Snapshot
from core.vendor_http import get_with_retry
from quant.aggregate import aggregate_bars

SNAPSHOT_BATCH = 200
"""Symbols per snapshot request.

Alpaca accepts more, but a chunk that fails takes its whole chunk with it, and
200 keeps one failure from costing a fifth of a thousand-name universe. It is a
configurable trade between request count and blast radius, not a vendor limit.
"""

BARS_BATCH = 100
"""Symbols per multi-symbol bar request.

Smaller than snapshots because each symbol brings a year of daily rows, and an
oversized chunk pages more times than it saves requests.
"""

_MAX_BAR_PAGES = 50
"""Ceiling on how many pages one bar request will follow.

Alpaca answers a bar request with a page and a `next_page_token`, and the page
is far smaller than the `limit` asks for — a 90-day hourly window came back as
207 bars out of roughly 630, with a token nobody read. Because the page is the
*oldest* part of the window, ignoring the token did not shorten the series, it
moved it: on 2026-08-31 the newest hourly bar for AAPL was from 8 July, and
every entry, stop and ATR the strategy drew from it described July's market.
The desk's own staleness gate never caught it, because that gate reads the
daily series, which fits in one page.

The ceiling is here so a bad window cannot loop forever; 50 pages covers years
of hourly data.
"""

ALPACA_TIMEFRAME: dict[Timeframe, str] = {
    Timeframe.M5: "5Min",
    Timeframe.M15: "15Min",
    Timeframe.H1: "1Hour",
    Timeframe.D1: "1Day",
}


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


_limiter: RateLimiter | None = None
_limiter_rpm: int | None = None


def _account_limiter() -> RateLimiter:
    """One token bucket for every request this key makes.

    The scanner's `market_data` budget paces *symbols*; this paces *requests*,
    and the gap between those two is where the 429s came from. Stage 3 runs four
    symbols at a time and each one paginates hourly bars up to a dozen times, so
    a budget of four concurrent symbols is a burst of roughly fifty requests
    against a quota of two hundred a minute.

    Module-level rather than per-adapter because the quota belongs to the API
    key. Two adapters — the scan cycle's and reconciliation's — are two callers
    of one account, and a limiter each would permit exactly twice the quota.
    """
    global _limiter, _limiter_rpm
    rpm = max(1, get_settings().market_data_requests_per_minute)
    if _limiter is None or _limiter_rpm != rpm:
        # A burst of one minute's worth would defeat the purpose; a small burst
        # keeps a batched read — a handful of large requests — from trickling.
        _limiter = RateLimiter(rpm / 60.0, burst=max(2.0, rpm / 20.0))
        _limiter_rpm = rpm
    return _limiter


def reset_account_limiter() -> None:
    """Drop the shared bucket. For tests and for a configuration change."""
    global _limiter, _limiter_rpm
    _limiter = None
    _limiter_rpm = None


def set_account_limiter(limiter: RateLimiter) -> None:
    """Install a bucket and keep it installed, for tests.

    Assigning `_limiter` alone does not hold: `_account_limiter` rebuilds
    whenever the installed bucket does not match the configured rate, so a bare
    assignment is discarded on the very next request.
    """
    global _limiter, _limiter_rpm
    _limiter = limiter
    _limiter_rpm = max(1, get_settings().market_data_requests_per_minute)


async def _paced_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Every read this adapter makes: paced first, then retried.

    Both halves matter and they fix different things. Pacing stops the burst
    that earns a 429; the retry means that a 429 arriving anyway — because
    another process shares the key, or the vendor is stricter than advertised —
    is a short wait rather than a failed symbol. Before this, the per-symbol bar
    path had neither, which is why a scan cycle could lose twenty names to rate
    limiting while the batched stages sailed through.
    """
    return await get_with_retry(
        client,
        url,
        params=params,
        headers=headers,
        before_attempt=_account_limiter().acquire,
    )


def _dec(value: Any) -> Decimal | None:
    """A number the feed may not have sent. Absent stays absent, never zero."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _bar_from_alpaca(symbol: str, timeframe: Timeframe, row: dict[str, Any]) -> Bar | None:
    """One row, or None if it is not a usable bar.

    A row missing a price or a timestamp is dropped rather than raising. Per
    symbol that distinction did not matter — one bad row failed one symbol's
    request. In a batch of a thousand it would fail the batch, so a single
    malformed record would take the entire universe's history out of the cycle
    and every name in it would be rejected for insufficient history.

    Dropping is safe in the direction that counts: fewer bars means Stage 2
    refuses the name for `INSUFFICIENT_HISTORY`, never that it scores it on
    substituted values.
    """
    ts = _ts(row.get("t"))
    if ts is None:
        return None

    open_ = _dec(row.get("o"))
    high = _dec(row.get("h"))
    low = _dec(row.get("l"))
    close = _dec(row.get("c"))
    volume = _dec(row.get("v"))
    if open_ is None or high is None or low is None or close is None or volume is None:
        return None

    return Bar(
        symbol=symbol.upper(),
        timeframe=timeframe,
        ts=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        source="alpaca",
    )


def _snapshot_from_alpaca(symbol: str, raw: dict[str, Any]) -> Snapshot:
    trade = raw.get("latestTrade") or {}
    quote = raw.get("latestQuote") or {}
    daily = raw.get("dailyBar") or {}
    prev = raw.get("prevDailyBar") or {}
    return Snapshot(
        symbol=symbol.upper(),
        price=_dec(trade.get("p")) or _dec(daily.get("c")),
        bid=_dec(quote.get("bp")),
        ask=_dec(quote.get("ap")),
        day_volume=_dec(daily.get("v")),
        day_high=_dec(daily.get("h")),
        day_low=_dec(daily.get("l")),
        prev_close=_dec(prev.get("c")),
        trade_ts=_ts(trade.get("t")) or _ts(daily.get("t")),
        quote_ts=_ts(quote.get("t")),
        source="alpaca",
    )


class AlpacaMarketData:
    source = "alpaca"

    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
        }

    async def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        symbol = symbol.upper()
        if timeframe == Timeframe.H4:
            hourly = await self.get_bars(symbol, Timeframe.H1, start, end)
            return aggregate_bars(hourly, Timeframe.H4, self.source)

        alpaca_tf = ALPACA_TIMEFRAME.get(timeframe)
        if alpaca_tf is None:
            raise ValueError(f"unsupported timeframe {timeframe}")

        params: dict[str, str | int] = {
            "timeframe": alpaca_tf,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "adjustment": "raw",
            "feed": "iex",
            "limit": 10000,
        }
        url = f"{self._base}/v2/stocks/{symbol}/bars"
        out: list[Bar] = []
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            page = dict(params)
            for _ in range(_MAX_BAR_PAGES):
                resp = await _paced_get(client, url, headers=self._headers(), params=page)
                payload = resp.json()

                for row in payload.get("bars") or []:
                    out.append(
                        Bar(
                            symbol=symbol,
                            timeframe=timeframe,
                            ts=datetime.fromisoformat(row["t"]),
                            open=Decimal(str(row["o"])),
                            high=Decimal(str(row["h"])),
                            low=Decimal(str(row["l"])),
                            close=Decimal(str(row["c"])),
                            volume=Decimal(str(row["v"])),
                            source=self.source,
                        )
                    )

                token = payload.get("next_page_token")
                if not token:
                    return out
                page = {**params, "page_token": token}

        # Falling out of the loop means the window is larger than we will page
        # through. Returning what we have would return the *oldest* part of it,
        # which is the failure this loop exists to remove, so say so instead.
        raise RuntimeError(f"ALPACA_BARS_TOO_MANY_PAGES:{symbol}:{alpaca_tf}")

    async def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        """Today's picture for many symbols, in requests counted in the dozens.

        This is the single change that makes a large universe affordable. The
        per-symbol path costs roughly 12 HTTP requests and 4 seconds; this
        serves `SNAPSHOT_BATCH` names per request, so a thousand of them cost
        about five requests and a couple of seconds.

        Symbols the feed omits are simply absent from the result. That is
        deliberate: a missing snapshot must reach Stage 1 as missing, not as a
        `Snapshot` full of `None` that reads like a successful read of an empty
        book.
        """
        wanted = [s.upper() for s in symbols if s]
        if not wanted:
            return {}

        out: dict[str, Snapshot] = {}
        url = f"{self._base}/v2/stocks/snapshots"
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            for chunk in _chunks(wanted, SNAPSHOT_BATCH):
                params = {"symbols": ",".join(chunk), "feed": "iex"}
                resp = await _paced_get(client, url, params=params, headers=self._headers())
                resp.raise_for_status()
                payload = resp.json()
                # Alpaca has served this either bare or wrapped in "snapshots"
                # depending on endpoint version; accept both rather than return
                # an empty batch that looks like a thousand illiquid symbols.
                records = payload.get("snapshots") if isinstance(payload, dict) else None
                if records is None:
                    records = payload if isinstance(payload, dict) else {}
                for symbol, raw in records.items():
                    if isinstance(raw, dict):
                        out[str(symbol).upper()] = _snapshot_from_alpaca(str(symbol), raw)
        return out

    async def get_daily_bars_batch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[Bar]]:
        """Daily history for many symbols at once, for ADV and quant features.

        Paginates the same way the single-symbol path does, and for the same
        reason: a page is the *oldest* part of the window, so a token left
        unread does not shorten the series, it moves it into the past.
        """
        wanted = [s.upper() for s in symbols if s]
        if not wanted:
            return {}

        out: dict[str, list[Bar]] = {}
        url = f"{self._base}/v2/stocks/bars"
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            for chunk in _chunks(wanted, BARS_BATCH):
                params: dict[str, str | int] = {
                    "symbols": ",".join(chunk),
                    "timeframe": "1Day",
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "adjustment": "raw",
                    "feed": "iex",
                    "limit": 10000,
                }
                page = dict(params)
                for _ in range(_MAX_BAR_PAGES):
                    resp = await _paced_get(client, url, params=page, headers=self._headers())
                    resp.raise_for_status()
                    payload = resp.json()
                    for symbol, rows in (payload.get("bars") or {}).items():
                        bucket = out.setdefault(str(symbol).upper(), [])
                        for row in rows or []:
                            bar = _bar_from_alpaca(str(symbol), Timeframe.D1, row)
                            if bar is not None:
                                bucket.append(bar)
                    token = payload.get("next_page_token")
                    if not token:
                        break
                    page = {**params, "page_token": token}
                else:
                    raise RuntimeError(f"ALPACA_BARS_TOO_MANY_PAGES:batch:{len(chunk)}")
        for bars in out.values():
            bars.sort(key=lambda b: b.ts)
        return out

    async def get_quote(self, symbol: str) -> Quote | None:
        """Live top of book — the only input a spread check may be built on.

        Returns None rather than a fabricated quote when the feed has nothing:
        the liquidity gate is designed to fail closed on None, and inventing a
        bid/ask here would defeat that.
        """
        symbol = symbol.upper()
        url = f"{self._base}/v2/stocks/{symbol}/quotes/latest"
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            # Retried like everything else, and for the sharpest reason: the
            # liquidity gate fails closed on a missing quote, so a dropped
            # request here is a refused trade rather than a missing number.
            resp = await _paced_get(client, url, headers=self._headers(), params={"feed": "iex"})
            payload = resp.json()

        raw = payload.get("quote") or {}
        bid, ask = raw.get("bp"), raw.get("ap")
        if not bid or not ask:
            return None
        return Quote(
            symbol=symbol,
            bid=Decimal(str(bid)),
            ask=Decimal(str(ask)),
            bid_size=Decimal(str(raw["bs"])) if raw.get("bs") is not None else None,
            ask_size=Decimal(str(raw["as"])) if raw.get("as") is not None else None,
            ts=datetime.fromisoformat(raw["t"]),
            source=self.source,
        )

    async def get_last_price(self, symbol: str) -> float:
        symbol = symbol.upper()
        url = f"{self._base}/v2/stocks/{symbol}/trades/latest"
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await _paced_get(client, url, headers=self._headers(), params={"feed": "iex"})
            payload = resp.json()
        return float(payload["trade"]["p"])
