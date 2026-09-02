"""
Retrying GET for read-only vendor endpoints, and a safe way to name a failure.

Both Finnhub callers — the news agent, the earnings calendar, and the sector
resolver — feed gates that fail closed. A single dropped request there is not a
lost data point, it is a refused trade: the calendar's answer decides whether
the risk engine will take the entry at all. A 503 is transient by definition, so
making exactly one attempt and reporting "unavailable" converts a two-second
vendor hiccup into a skipped setup.

Only idempotent reads belong here. Retrying an order is a way to send two.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from core.vendor_quota import (
    fallback_429_wait,
    parse_rate_limit_headers,
    wait_seconds_from_headers,
)

# 5xx is the vendor failing to answer a question it accepts; 429 is it asking us
# to slow down. Both pass with time. Every other 4xx is a statement about the
# request itself — a bad key, an unknown symbol, a malformed window — and a
# second identical request only spends quota to be told the same thing.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ATTEMPTS = 3
BASE_DELAY = 0.4


def describe_http_error(exc: BaseException) -> str:
    """
    Name a vendor failure without quoting the vendor.

    Finnhub renders the full request URL into `HTTPStatusError.__str__`, which
    is how an API key passed as `token=` reaches the desk log and the audit
    trail. The status code carries the part an operator actually needs — 503 is
    an outage to wait out, 401 is a key to fix, and the two call for opposite
    responses — and a status code is not a secret.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def retry_delay_seconds(exc: BaseException, *, attempt: int, base_delay: float) -> float:
    """How long to wait before the next attempt.

    429 prefers the vendor's Reset / Retry-After. Everything else stays a short
    exponential — a blip 503 is not a rate-limit window.
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        parsed = parse_rate_limit_headers(exc.response.headers)
        header_wait = wait_seconds_from_headers(parsed)
        if header_wait is not None:
            return max(0.0, header_wait)
        return float(fallback_429_wait(attempt))
    return float(base_delay * (2**attempt))


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = ATTEMPTS,
    base_delay: float = BASE_DELAY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
    after_response: Callable[[httpx.Response], Awaitable[None] | None] | None = None,
    on_retryable: Callable[[BaseException, int], Awaitable[None]] | None = None,
) -> httpx.Response:
    """
    GET `url`, retrying transport failures and retryable statuses.

    Raises the last failure once the attempts are spent, so the caller still
    decides what an exhausted retry means for its gate.

    `before_attempt` runs ahead of every attempt, including retries — where a
    caller puts its account quota. Pacing the first attempt only would let a
    throttled endpoint be retried at full speed.

    `after_response` runs on every HTTP response before status is raised, so
    rate-limit headers on both 200 and 429 reach the quota tracker.

    `on_retryable(exc, attempt)` runs after a retryable failure is caught and
    before the backoff sleep.
    """
    last: httpx.HTTPError | None = None
    for attempt in range(attempts):
        try:
            if before_attempt is not None:
                await before_attempt()
            response = await client.get(url, params=params, headers=headers)
            if after_response is not None:
                maybe = after_response(response)
                if maybe is not None:
                    await maybe
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in RETRY_STATUS:
                raise
            last = exc
        except httpx.TransportError as exc:
            last = exc

        if on_retryable is not None:
            await on_retryable(last, attempt)

        if attempt < attempts - 1:
            await sleep(retry_delay_seconds(last, attempt=attempt, base_delay=base_delay))

    assert last is not None  # only reachable after a caught failure
    raise last
