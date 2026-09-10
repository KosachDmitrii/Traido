"""
Evaluation endpoints.

Read-only measurement surface: the desk can ask "is this edge real?" without
any path to the broker. Evaluations are cached because a walk-forward run is
expensive, and the answer does not change minute to minute.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Path, Query

from core.config import get_settings
from core.enums import Timeframe
from core.universe import default_universe
from market_data.factory import create_market_data_port
from market_data.providers.company_name import attach_company_names, get_company_name_resolver
from quant.backtesting.service import MarketDataUnavailable, evaluate_symbol
from strategy.orb.evaluation import paper_evaluation
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


@router.get("/evaluation/orb-paper")
async def orb_paper_evaluation() -> dict:
    """Actual journal outcomes for the active ORB version only."""
    return paper_evaluation()


@router.get("/evaluation/{symbol}")
async def evaluation(
    symbol: str,
    timeframe: Timeframe = Timeframe.D1,
    refresh: bool = Query(default=False, description="Bypass the cache and recompute"),
    strategy: str = Query(
        default="desk",
        description="desk = trader_desk (same as paper/live stamp); stub = ema research",
    ),
) -> dict:
    settings = get_settings()
    try:
        result = await evaluate_symbol(
            symbol,
            market_data=create_market_data_port(settings),
            timeframe=timeframe,
            benchmark=default_universe().benchmark,
            use_cache=not refresh,
            strategy=strategy,
        )
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = result.as_dict()
    names = await get_company_name_resolver(settings.finnhub_api_key).resolve_many(
        [payload.get("symbol") or symbol]
    )
    payload["name"] = names.get(str(payload.get("symbol") or symbol).upper())
    return payload


@router.get("/evaluation")
async def evaluation_batch(
    symbols: str = Query(description="Comma-separated symbols"),
    timeframe: Timeframe = Timeframe.D1,
    strategy: str = Query(default="desk"),
) -> dict:
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:MAX_BATCH]
    if not requested:
        raise HTTPException(status_code=422, detail="no symbols requested")

    settings = get_settings()
    market_data = create_market_data_port(settings)
    benchmark = default_universe().benchmark

    outcomes = await asyncio.gather(
        *(
            evaluate_symbol(
                sym,
                market_data=market_data,
                timeframe=timeframe,
                benchmark=benchmark,
                strategy=strategy,
            )
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

    await attach_company_names(results, settings.finnhub_api_key)
    return {"results": results, "errors": errors}


@router.get("/evaluation/orb-symbol/{symbol}")
async def orb_symbol(symbol: str = Path(pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,15}$")) -> dict:
    """Inspect a saved ORB decision and quote without evaluating or publishing entries."""
    from core.clock import market_date
    from strategy.orb.runtime import STATUS
    from strategy.orb.store import read_session

    symbol = symbol.upper()
    snapshot = read_session(str(market_date())) or dict(STATUS)
    plan = snapshot.get("plans", {}).get(symbol)
    state = snapshot.get("states", {}).get(symbol)
    reasons = snapshot.get("rejections", {}).get(symbol, [])
    outranked = symbol in snapshot.get("outranked", [])
    quote = None
    quote_error = None
    try:
        quote = await create_market_data_port(get_settings()).get_quote(symbol)
        if quote is None:
            quote_error = "ORB_QUOTE_MISSING"
    except Exception:  # noqa: BLE001
        quote_error = "ORB_SERVICE_UNAVAILABLE"
    return {
        "symbol": symbol,
        "session": snapshot.get("session"),
        "plan": {k: v for k, v in plan.items() if k != "evidence"} if plan else None,
        "state": state,
        "rejections": reasons,
        "outranked": outranked,
        "session_reason": snapshot.get("reason"),
        "quote": quote.model_dump(mode="json") if quote else None,
        "quote_error": quote_error,
    }
