"""Stage 3/4 scan API — kept for debugging; product path is auto scanner + /desk."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.enums import Timeframe
from core.schemas import PipelineResult
from trading.pipeline import run_symbol_pipeline

router = APIRouter(prefix="/api/v1", tags=["scan"])


class ScanRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    timeframes: list[str] = Field(default_factory=lambda: ["1d", "1h"])


async def _run_scan(symbol: str, timeframes: list[str]) -> PipelineResult:
    tfs: list[Timeframe] = []
    for raw in timeframes:
        try:
            tfs.append(Timeframe(raw))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid timeframe: {raw}") from exc
    if not tfs:
        raise HTTPException(status_code=400, detail="At least one timeframe required")
    return await run_symbol_pipeline(symbol, timeframes=tuple(tfs))


@router.post("/scan", response_model=PipelineResult)
async def scan_symbol(body: ScanRequest) -> PipelineResult:
    return await _run_scan(body.symbol, body.timeframes)


@router.get("/scan/{symbol}", response_model=PipelineResult)
async def scan_symbol_get(
    symbol: str,
    timeframe: list[str] = Query(default=["1d", "1h"]),
) -> PipelineResult:
    return await _run_scan(symbol, timeframe)
