"""AccountQuota must obey the vendor's contract, not guessed sleeps."""

from __future__ import annotations

import time

import httpx
import pytest

from core.vendor_http import retry_delay_seconds
from core.vendor_quota import (
    AccountQuota,
    parse_rate_limit_headers,
    wait_seconds_from_headers,
)


def test_parse_rate_limit_headers_reads_alpaca_names() -> None:
    parsed = parse_rate_limit_headers(
        {
            "X-RateLimit-Limit": "200",
            "X-RateLimit-Remaining": "3",
            "X-RateLimit-Reset": "1700000000",
        }
    )
    assert parsed.limit == 200
    assert parsed.remaining == 3
    assert parsed.reset_epoch == 1700000000.0


def test_wait_seconds_prefers_retry_after() -> None:
    parsed = parse_rate_limit_headers({"Retry-After": "12", "X-RateLimit-Reset": "1"})
    assert wait_seconds_from_headers(parsed, now_epoch=0) == 12.0


def test_wait_seconds_uses_reset_epoch() -> None:
    now = 1_700_000_000.0
    parsed = parse_rate_limit_headers({"X-RateLimit-Reset": str(now + 8)})
    assert wait_seconds_from_headers(parsed, now_epoch=now) == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_quota_cools_until_reset_after_429() -> None:
    quota = AccountQuota(rpm=200)
    reset = time.time() + 0.25
    wait = await quota.note_throttled(
        {
            "X-RateLimit-Limit": "200",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset),
            "Retry-After": "0.25",
        },
        attempt=0,
    )
    assert wait >= 0.2
    assert quota.seconds_until_clear() >= 0.15
    started = time.monotonic()
    await quota.acquire()
    assert time.monotonic() - started >= 0.15


@pytest.mark.asyncio
async def test_quota_does_not_spin_after_reset_elapses() -> None:
    """Stale Remaining=0 after Reset must not block forever."""
    quota = AccountQuota(rpm=200)
    await quota.note_throttled(
        {
            "X-RateLimit-Limit": "200",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(time.time() - 1),
            "Retry-After": "0",
        },
        attempt=0,
    )
    started = time.monotonic()
    await quota.acquire()
    assert time.monotonic() - started < 0.5


def test_retry_delay_honors_header_on_429() -> None:
    request = httpx.Request("GET", "https://data.example/v2/x")
    response = httpx.Response(
        429,
        request=request,
        headers={"Retry-After": "7"},
    )
    exc = httpx.HTTPStatusError("throttled", request=request, response=response)
    assert retry_delay_seconds(exc, attempt=0, base_delay=0.4) == 7.0


def test_retry_delay_for_503_stays_short() -> None:
    request = httpx.Request("GET", "https://data.example/v2/x")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("blip", request=request, response=response)
    assert retry_delay_seconds(exc, attempt=1, base_delay=0.4) == pytest.approx(0.8)
