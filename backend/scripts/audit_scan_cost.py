"""Measure what one scanned symbol actually costs, in requests and in seconds.

Phase 0 of the universe refactor: no number in the audit may be estimated when
it can be counted. Counts live HTTP by host by wrapping the transport every
provider ends up using, so a call cannot avoid the tally by building its own
client — which several of them do.

Read-only. Runs the pipeline with `publish=False`, so nothing reaches the desk
and nothing reaches a broker.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter

import httpx

REQUESTS: Counter[str] = Counter()
_real_send = httpx.AsyncClient.send


async def _counting_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
    REQUESTS[request.url.host] += 1
    return await _real_send(self, request, **kwargs)


httpx.AsyncClient.send = _counting_send  # type: ignore[method-assign]


async def main(symbols: list[str]) -> None:
    from trading.pipeline import run_symbol_pipeline
    from trading.scan_context import open_scan_context

    async with open_scan_context() as ctx:
        # One warm-up symbol first: process-wide caches (earnings, broker) would
        # otherwise be charged to whichever symbol happened to run first.
        print(f"{'symbol':8}{'status':22}{'seconds':>9}   requests by host")
        for symbol in symbols:
            REQUESTS.clear()
            t0 = time.monotonic()
            try:
                result = await run_symbol_pipeline(symbol, publish=False, context=ctx)
                status = result.status
            except Exception as exc:
                status = f"EXC {type(exc).__name__}"
            dt = time.monotonic() - t0
            hosts = ", ".join(f"{h.split('.')[-2]}={n}" for h, n in sorted(REQUESTS.items()))
            print(
                f"{symbol:8}{status:22}{dt:9.2f}   {hosts or '(none)'}  total={sum(REQUESTS.values())}"
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["AAPL", "MSFT", "NVDA", "XLE", "KO"]))
