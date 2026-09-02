"""Measure the scan funnel at 100, 500 and 1000 instruments.

Runs the real `run_cycle` against the deterministic fakes in
`tests/scanner_fakes.py`. No network, so the numbers are the pipeline's own cost
rather than a vendor's latency on the day — which is the point: a benchmark that
moves with someone else's API is not a budget, it is weather.

What this does *not* measure is live provider latency. That is the separate
operational benchmark (`scripts/audit_scan_cost.py`), which does hit the network
and is not run in CI.

    PYTHONPATH=. python scripts/benchmark_scanner.py
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.scanner import cycle as scan_cycle
from core.enums import Timeframe
from tests.scanner_fakes import (
    FakeMarketData,
    fake_scan_context,
    make_symbol,
    scanner_settings,
    universe_service_for,
)

SIZES = (100, 500, 1000)


class _Desk:
    """Stands in for the opportunity store and the pipeline's expensive half."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.deep_calls = 0

    def list_open(self) -> list[object]:
        return []


def _pipeline_for(desk: _Desk):
    """Stand in for Stage 3, counting how many names actually reach it.

    The count is the benchmark's whole point: it must stay flat while the
    universe grows, or the funnel is not doing its job.
    """

    async def _pipeline(symbol: str, **_kwargs: Any) -> Any:
        desk.deep_calls += 1
        return _no_candidate(symbol)

    return _pipeline


async def _measure(size: int, *, repeats: int) -> dict[str, Any]:
    settings = scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=20,
        TRAIDO_MAX_LLM_CANDIDATES=20,
    )
    symbols = [make_symbol(i) for i in range(size)]
    service = universe_service_for(symbols)
    market_data = FakeMarketData()

    runs: list[Any] = []
    for _ in range(repeats):
        desk = _Desk()

        stub = _pipeline_for(desk)
        ctx = fake_scan_context(settings, market_data=market_data)
        with (
            patch.object(scan_cycle, "run_symbol_pipeline", stub),
            patch.object(scan_cycle.OPPORTUNITIES, "list_open", desk.list_open),
            patch.object(scan_cycle, "_held_symbols", set),
            patch.object(scan_cycle, "withdraw_unactionable", lambda _store: None),
        ):
            started = time.perf_counter()
            result = await scan_cycle.run_cycle(
                settings=settings,
                universe_service=service,
                timeframes=(Timeframe.D1,),
                max_open=5,
                scheduled_at=datetime.now(UTC),
                context=ctx,
            )
            wall = time.perf_counter() - started
        await ctx.aclose()
        runs.append((result, wall, desk.deep_calls))

    result, _, deep_calls = runs[-1]
    timings = [r[0].timings for r in runs]
    return {
        "size": size,
        "universe_total": result.funnel.universe_total,
        "eligible": result.funnel.structurally_eligible,
        "stage1_passed": result.funnel.market_filter_passed,
        "quant_shortlisted": result.funnel.quant_shortlisted,
        "deep_started": result.funnel.deep_analysis_started,
        "deep_calls": deep_calls,
        "reconciles": result.funnel.reconciles(),
        # Cold and warm are both worth knowing: Stage 0 is a cached reference
        # read, so the warm number is what almost every cycle actually pays and
        # the cold one is what a universe refresh costs.
        "stage0_cold_ms": round(timings[0].universe * 1000.0, 1),
        "stage0_ms": _median(t.universe for t in timings[1:] or timings),
        "stage1_ms": _median(t.market_filter for t in timings),
        "quant_ms": _median(t.prerank for t in timings),
        "deep_ms": _median(t.deep_analysis for t in timings),
        "total_ms": _median(t.total for t in timings),
        "wall_ms": _median(r[1] for r in runs),
        "batch_requests": (market_data.snapshot_requests + market_data.bar_requests) // repeats,
        "per_symbol_calls": len(market_data.per_symbol_bar_calls) // repeats,
    }


def _median(values: Any) -> float:
    return round(statistics.median(list(values)) * 1000.0, 1)


def _no_candidate(symbol: str) -> Any:
    from core.schemas import PipelineResult

    return PipelineResult(symbol=symbol, status="no_candidate")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sizes", type=int, nargs="*", default=list(SIZES))
    args = parser.parse_args()

    rows = [await _measure(size, repeats=args.repeats) for size in args.sizes]

    header = (
        f"{'symbols':>8} {'eligible':>9} {'stage1':>7} {'quant':>6} {'deep':>5} "
        f"{'s0 cold':>8} {'s0 warm':>8} {'s1 ms':>7} {'s2 ms':>7} {'s3 ms':>7} "
        f"{'total ms':>9} {'reqs':>5}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['size']:>8} {row['eligible']:>9} {row['stage1_passed']:>7} "
            f"{row['quant_shortlisted']:>6} {row['deep_started']:>5} "
            f"{row['stage0_cold_ms']:>8} {row['stage0_ms']:>8} {row['stage1_ms']:>7} "
            f"{row['quant_ms']:>7} {row['deep_ms']:>7} {row['total_ms']:>9} "
            f"{row['batch_requests']:>5}"
        )

    print()
    for row in rows:
        assert row["reconciles"], f"funnel did not balance at {row['size']}"
        # The scalability claim in one line: the expensive stage is flat while
        # the universe grows tenfold.
        print(
            f"{row['size']:>5} instruments → {row['deep_calls']} deep analyses, "
            f"{row['batch_requests']} batch reads, "
            f"{row['per_symbol_calls']} per-symbol bar reads, funnel balanced"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
