"""
Evaluation endpoints.

Read-only measurement surface: the desk can ask "is this edge real?" without
any path to the broker. Evaluations are cached because a walk-forward run is
expensive, and the answer does not change minute to minute.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from core.config import get_settings
from core.enums import Timeframe
from core.universe import default_universe
from market_data.factory import create_market_data_port
from quant.backtesting.service import MarketDataUnavailable, evaluate_symbol
from trading.f3_diagnostics import write_forward_report
from trading.historical_mfe import ensure_seeded_from_aftermath, sync_from_paper_journal

router = APIRouter(prefix="/api/v1", tags=["evaluation"])

MAX_BATCH = 10


@router.get("/diagnostics/f3")
async def diagnostics_f3() -> dict:
    """F3 measurement surface: signal / wait / target / forward Paper progress.

    Read-only for capital. Side effects are limited to ensuring the historical
    MFE corpus is seeded, syncing closed-journal MFE, and refreshing the local
    forward report — never broker or order paths.
    """
    ensure_seeded_from_aftermath()
    sync_from_paper_journal()
    return write_forward_report()


@router.get("/evaluation/{symbol}")
async def evaluation(
    symbol: str,
    timeframe: Timeframe = Timeframe.D1,
    refresh: bool = Query(default=False, description="Bypass the cache and recompute"),
) -> dict:
    settings = get_settings()
    try:
        result = await evaluate_symbol(
            symbol,
            market_data=create_market_data_port(settings),
            timeframe=timeframe,
            benchmark=default_universe().benchmark,
            use_cache=not refresh,
        )
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.as_dict()


@router.get("/evaluation")
async def evaluation_batch(
    symbols: str = Query(description="Comma-separated symbols"),
    timeframe: Timeframe = Timeframe.D1,
) -> dict:
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:MAX_BATCH]
    if not requested:
        raise HTTPException(status_code=422, detail="no symbols requested")

    settings = get_settings()
    market_data = create_market_data_port(settings)
    benchmark = default_universe().benchmark

    outcomes = await asyncio.gather(
        *(
            evaluate_symbol(sym, market_data=market_data, timeframe=timeframe, benchmark=benchmark)
            for sym in requested
        ),
        return_exceptions=True,
    )

    results = []
    errors = {}
    for sym, outcome in zip(requested, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            errors[sym] = str(outcome)
        else:
            results.append(outcome.as_dict())

    return {"results": results, "errors": errors}
