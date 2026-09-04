"""Strategy registry + promotion gate API (Stage 8).

Read and human-authorize strategy versions. Nothing here places orders.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from strategy.promotion import (
    PromotionError,
    human_approve,
    promote_to_production,
    recompute_version,
    reject_version,
)
from strategy.registry import (
    ensure_builtin_strategies,
    get_by_id,
    list_versions,
    production_versions,
)
from strategy.thresholds import get_promotion_thresholds

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


class ActorBody(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=128)


class RejectBody(ActorBody):
    reason: str = Field(min_length=3, max_length=2000)


@router.get("")
def strategies_list() -> dict:
    ensure_builtin_strategies()
    return {
        "thresholds": get_promotion_thresholds().as_dict(),
        "versions": list_versions(),
        "production": production_versions(),
    }


@router.get("/{version_id}")
def strategy_detail(version_id: UUID) -> dict:
    ensure_builtin_strategies()
    row = get_by_id(version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy version not found")
    return {"thresholds": get_promotion_thresholds().as_dict(), "version": row}


@router.post("/{version_id}/recompute")
def strategy_recompute(version_id: UUID) -> dict:
    try:
        version = recompute_version(version_id)
    except PromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version": version, "thresholds": get_promotion_thresholds().as_dict()}


@router.post("/{version_id}/approve")
def strategy_approve(version_id: UUID, body: ActorBody) -> dict:
    try:
        version = human_approve(version_id, actor=body.actor.strip() or "operator")
    except PromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version": version}


@router.post("/{version_id}/promote")
def strategy_promote(version_id: UUID, body: ActorBody) -> dict:
    try:
        version = promote_to_production(version_id, actor=body.actor.strip() or "operator")
    except PromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version": version}


@router.post("/{version_id}/reject")
def strategy_reject(version_id: UUID, body: RejectBody) -> dict:
    try:
        version = reject_version(
            version_id,
            actor=body.actor.strip() or "operator",
            reason=body.reason.strip(),
        )
    except PromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version": version}
