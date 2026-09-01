"""The scan cycle must not earn its own 429s.

Live on 2026-08-31, a 176-name cycle finished with `risk-passed 0 · published 0`
and a desk log full of `429 Too Many Requests` against the hourly bar endpoint.
Nothing was wrong with the funnel: Stage 3 ran four symbols at a time, each one
paginated hourly bars a dozen times, and those requests were issued as fast as
the event loop could write them. Four concurrent symbols was a burst of roughly
fifty requests against a quota of two hundred a minute.

Two separate defects made that outcome possible, and both are covered here:

  1. The per-symbol bar path called `client.get` directly, so a 429 was fatal
     where every other vendor read in the codebase retries.
  2. The scanner's `market_data` budget paces *symbols*. Nothing paced
     *requests*, and a symbol is not a request — it is thirteen of them.

The consequence is not slowness. Deep analysis feeds the strategy, so a symbol
whose bars 429 is a symbol the desk never evaluates, and a cycle can report a
clean funnel while the reason it published nothing is that it was throttled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.concurrency import RateLimiter
from core.enums import Timeframe
from market_data.providers import alpaca as alpaca_module
from market_data.providers.alpaca import AlpacaMarketData

START = datetime(2026, 6, 1, tzinfo=UTC)
END = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


def _bar(ts: datetime) -> dict[str, object]:
    return {"t": ts.isoformat().replace("+00:00", "Z"), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1000}


def _adapter() -> AlpacaMarketData:
    return AlpacaMarketData(api_key="k", api_secret="s", base_url="https://data.example.test")


def _install(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    original = httpx.AsyncClient

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alpaca_module.httpx, "AsyncClient", client)


@pytest.mark.asyncio
async def test_a_throttled_bar_request_is_retried_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 means "slow down", not "this symbol has no bars"."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"message": "too many requests"})
        return httpx.Response(200, json={"bars": [_bar(START)]})

    _install(monkeypatch, handler)

    bars = await _adapter().get_bars("AAPL", Timeframe.H1, START, END)

    assert attempts["n"] == 2
    assert len(bars) == 1


@pytest.mark.asyncio
async def test_a_bad_key_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 answers the same way twice; spending quota to hear it again is waste."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"message": "unauthorized"})

    _install(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await _adapter().get_bars("AAPL", Timeframe.H1, START, END)

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_concurrent_symbols_share_one_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression itself.

    Four symbols paginating at once must not put more requests in flight than
    the account allows. Asserted on the bucket rather than on elapsed time, so
    the test measures the limit and not the machine it runs on.
    """
    pages = 4

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("page_token")
        index = 0 if token is None else int(token)
        body: dict[str, object] = {"bars": [_bar(START + timedelta(hours=index))]}
        if index + 1 < pages:
            body["next_page_token"] = str(index + 1)
        return httpx.Response(200, json=body)

    _install(monkeypatch, handler)

    counting = _CountingLimiter(1e6, burst=1e6)
    alpaca_module.set_account_limiter(counting)

    adapter = _adapter()
    await asyncio.gather(
        *(adapter.get_bars(sym, Timeframe.H1, START, END) for sym in ("A", "B", "C", "D"))
    )

    # Four symbols, four pages each: every request passed the bucket, none
    # slipped around it. An unpaced call site would show up here as a shortfall.
    assert counting.acquired == 4 * pages


@pytest.mark.asyncio
async def test_the_quote_read_is_paced_and_retried_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The liquidity gate fails closed, so a dropped quote is a refused trade."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(
            200,
            json={
                "quote": {
                    "bp": 100.0,
                    "ap": 100.05,
                    "t": START.isoformat().replace("+00:00", "Z"),
                }
            },
        )

    _install(monkeypatch, handler)
    counting = _CountingLimiter(1e6, burst=1e6)
    alpaca_module.set_account_limiter(counting)

    quote = await _adapter().get_quote("AAPL")

    assert quote is not None
    assert attempts["n"] == 2
    assert counting.acquired == 2


def test_the_bucket_belongs_to_the_account_not_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two adapters, one API key, one quota.

    The scan cycle and reconciliation each build their own adapter. A limiter
    per instance would permit exactly twice what the vendor allows, which is the
    same bug wearing a limiter.
    """
    alpaca_module.reset_account_limiter()

    first = alpaca_module._account_limiter()
    second = alpaca_module._account_limiter()

    assert first is second


class _CountingLimiter(RateLimiter):
    """A bucket that records how many requests asked it for permission."""

    def __init__(self, rate_per_sec: float, *, burst: float | None = None) -> None:
        super().__init__(rate_per_sec, burst=burst)
        self.acquired = 0

    async def acquire(self, tokens: float = 1.0) -> None:
        self.acquired += 1
        await super().acquire(tokens)
