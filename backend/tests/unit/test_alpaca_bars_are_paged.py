"""Alpaca answers a bar request with a page, not the series.

The response carries `next_page_token` and the page holds the *oldest* part of
the requested window. So a reader that takes the first page and stops does not
get a shorter series — it gets an older one, silently, with a shape that looks
entirely normal.

Live on 2026-08-31 the newest hourly bar for AAPL came back stamped 8 July,
seven weeks stale, while the quote feed was current to the second. Everything
the strategy computes from the execution timeframe — the close, SMA20, ATR, and
therefore every entry, stop and target — described July's market. The daily
series was correct throughout, because 62 bars fit in one page, which is why
the desk's staleness gate never saw it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.enums import Timeframe
from market_data.providers import alpaca as alpaca_module
from market_data.providers.alpaca import AlpacaMarketData

START = datetime(2026, 6, 1, tzinfo=UTC)
END = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


def _bar(ts: datetime, close: float) -> dict[str, object]:
    return {
        "t": ts.isoformat().replace("+00:00", "Z"),
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": 1000,
    }


class _PagedFeed:
    """Three pages, oldest first, exactly as Alpaca serves them."""

    def __init__(self, pages: int = 3, per_page: int = 4) -> None:
        self.pages = pages
        self.per_page = per_page
        self.requested_tokens: list[str | None] = []

    def _page_index(self, request: httpx.Request) -> int:
        token = request.url.params.get("page_token")
        self.requested_tokens.append(token)
        return 0 if token is None else int(token)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            index = self._page_index(request)
            first = index * self.per_page
            bars = [
                _bar(START + timedelta(hours=first + i), 100.0 + first + i)
                for i in range(self.per_page)
            ]
            body: dict[str, object] = {"bars": bars}
            if index + 1 < self.pages:
                body["next_page_token"] = str(index + 1)
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler)


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> _PagedFeed:
    feed = _PagedFeed()
    original = httpx.AsyncClient

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = feed.transport()
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alpaca_module.httpx, "AsyncClient", client)
    return feed


def _adapter() -> AlpacaMarketData:
    return AlpacaMarketData(api_key="k", api_secret="s", base_url="https://data.example.test")


@pytest.mark.asyncio
async def test_every_page_is_read(paged: _PagedFeed) -> None:
    bars = await _adapter().get_bars("AAPL", Timeframe.H1, START, END)

    assert len(bars) == paged.pages * paged.per_page
    assert paged.requested_tokens == [None, "1", "2"]


@pytest.mark.asyncio
async def test_the_newest_bar_survives(paged: _PagedFeed) -> None:
    """The point of the whole thing.

    One page alone ends at the oldest quarter of the window, and a series that
    ends seven weeks ago is not a shorter series, it is a different market.
    """
    bars = await _adapter().get_bars("AAPL", Timeframe.H1, START, END)

    newest = max(b.ts for b in bars)
    assert newest == START + timedelta(hours=paged.pages * paged.per_page - 1)


@pytest.mark.asyncio
async def test_a_window_too_large_to_page_is_an_error_not_a_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncating silently is the failure being removed; do not reintroduce it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": [_bar(START, 100.0)], "next_page_token": "more"})

    original = httpx.AsyncClient

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alpaca_module.httpx, "AsyncClient", client)

    with pytest.raises(RuntimeError, match="ALPACA_BARS_TOO_MANY_PAGES"):
        await _adapter().get_bars("AAPL", Timeframe.H1, START, END)
