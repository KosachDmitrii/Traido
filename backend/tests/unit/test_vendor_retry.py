"""
A transient vendor failure must not read as a verdict.

News and earnings both feed gates that fail closed, so "Finnhub did not answer"
and "Finnhub answered, and the news is bad" have the same effect on the trade:
no entry. That makes the difference between a real outage and a dropped request
worth spending a retry on, and makes caching the failure alongside the success
actively harmful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agents.news.agent import assess_news
from core.enums import EarningsCheck, NewsCheck
from core.vendor_http import describe_http_error, get_with_retry
from market_data.providers.earnings import EarningsCalendar

KEY = "sandbox_key_that_must_never_be_logged"
NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


class _Vendor:
    """A transport that plays a scripted sequence of statuses, then repeats."""

    def __init__(self, *statuses: int, body: object = None) -> None:
        self._statuses = list(statuses)
        self._body = body if body is not None else {"earningsCalendar": []}
        self.calls = 0

    def recovers(self) -> None:
        """The outage ends; every later request succeeds."""
        self._statuses = [200]

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            status = self._statuses[min(self.calls, len(self._statuses) - 1)]
            self.calls += 1
            if status == 200:
                return httpx.Response(200, json=self._body)
            # Finnhub renders the request URL into the error it raises, which is
            # the leak this suite also guards against.
            return httpx.Response(status, text=f"upstream said no: {request.url}")

        return httpx.MockTransport(handler)


async def _never_sleep(_: float) -> None:
    return None


# --- the helper itself -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_and_recovers() -> None:
    vendor = _Vendor(503, 200)
    async with httpx.AsyncClient(transport=vendor.transport()) as client:
        response = await get_with_retry(client, "https://x/y", sleep=_never_sleep)

    assert response.status_code == 200
    assert vendor.calls == 2, "the 503 should have been retried, not reported"


@pytest.mark.asyncio
async def test_a_rejected_key_is_not_retried() -> None:
    """401 is a statement about the request. Repeating it only spends quota."""
    vendor = _Vendor(401)
    async with httpx.AsyncClient(transport=vendor.transport()) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(client, "https://x/y", sleep=_never_sleep)

    assert vendor.calls == 1


@pytest.mark.asyncio
async def test_a_sustained_outage_gives_up_and_reports_the_last_failure() -> None:
    vendor = _Vendor(503)
    async with httpx.AsyncClient(transport=vendor.transport()) as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await get_with_retry(client, "https://x/y", attempts=3, sleep=_never_sleep)

    assert vendor.calls == 3
    assert caught.value.response.status_code == 503


@pytest.mark.asyncio
async def test_the_backoff_grows_between_attempts() -> None:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    vendor = _Vendor(503)
    async with httpx.AsyncClient(transport=vendor.transport()) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(client, "https://x/y", attempts=3, base_delay=0.4, sleep=record)

    # Three attempts, two waits between them — never a wait after the last.
    assert slept == [0.4, 0.8]


@pytest.mark.asyncio
async def test_a_429_backs_off_harder_than_a_blip() -> None:
    """Rate limits need seconds, not the short 503 backoff."""
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    vendor = _Vendor(429)
    async with httpx.AsyncClient(transport=vendor.transport()) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(client, "https://x/y", attempts=3, base_delay=0.4, sleep=record)

    assert slept == [5.0, 10.0]


def test_a_failure_is_named_by_status_not_by_the_vendors_message() -> None:
    request = httpx.Request("GET", f"https://finnhub.io/api/v1/x?token={KEY}")
    exc = httpx.HTTPStatusError(
        f"Server error for url https://finnhub.io/api/v1/x?token={KEY}",
        request=request,
        response=httpx.Response(503, request=request),
    )

    described = describe_http_error(exc)

    assert described == "HTTP 503"
    assert KEY not in described


def test_a_transport_failure_is_named_by_its_class() -> None:
    assert describe_http_error(httpx.ConnectTimeout("timed out")) == "ConnectTimeout"


# --- the earnings calendar ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_calendar_read_is_not_cached_for_the_whole_session() -> None:
    """
    The bug this closes: one 503 blackballed a symbol for six hours.

    A failed read used to be cached under the same six-hour TTL as a real
    answer, so a symbol whose calendar hiccuped once was refused every cycle
    until evening — long after Finnhub had recovered.
    """
    vendor = _Vendor(503)
    calendar = EarningsCalendar(
        KEY,
        failure_ttl=timedelta(minutes=2),
        transport=vendor.transport(),
    )

    first = await calendar.get("AAPL", now=NOW)
    assert first.status is EarningsCheck.UNAVAILABLE
    attempts_after_first = vendor.calls

    # Still inside the failure TTL: cached, so a burst of lookups cannot become
    # a burst of retries against a vendor already in trouble.
    await calendar.get("AAPL", now=NOW + timedelta(seconds=30))
    assert vendor.calls == attempts_after_first

    # Past it: tried again, well within the six hours the old TTL would have
    # waited. The vendor has recovered by now, and so does the symbol.
    vendor.recovers()
    recovered = await calendar.get("AAPL", now=NOW + timedelta(minutes=3))

    assert vendor.calls > attempts_after_first
    assert recovered.status is EarningsCheck.CHECKED


@pytest.mark.asyncio
async def test_a_successful_calendar_read_is_still_cached_for_hours() -> None:
    """The short TTL is for failures only — an earnings date does not move."""
    vendor = _Vendor(200)
    calendar = EarningsCalendar(KEY, transport=vendor.transport())

    await calendar.get("AAPL", now=NOW)
    await calendar.get("AAPL", now=NOW + timedelta(hours=5))

    assert vendor.calls == 1


@pytest.mark.asyncio
async def test_the_calendar_note_carries_the_status_code() -> None:
    """
    A 401 and a 503 call for opposite responses: fix the key, or wait it out.
    The note used to say only `HTTPStatusError`, which distinguishes neither.
    """
    vendor = _Vendor(401)
    calendar = EarningsCalendar(KEY, transport=vendor.transport())

    info = await calendar.get("AAPL", now=NOW)

    assert info.status is EarningsCheck.UNAVAILABLE
    assert "HTTP 401" in info.note
    assert KEY not in info.note


# --- the news agent ----------------------------------------------------------


@pytest.mark.asyncio
async def test_news_recovers_from_a_transient_failure() -> None:
    vendor = _Vendor(503, 200, body=[{"headline": "Quiet week"}])

    assessment = await assess_news("MU", KEY, transport=vendor.transport())

    assert assessment.status is NewsCheck.CHECKED
    assert vendor.calls == 2


@pytest.mark.asyncio
async def test_the_news_reason_carries_the_status_code() -> None:
    vendor = _Vendor(401)

    assessment = await assess_news("MU", KEY, transport=vendor.transport())

    assert assessment.status is NewsCheck.UNAVAILABLE
    assert "HTTP 401" in assessment.reasons[0]
    assert KEY not in " ".join(assessment.reasons)
