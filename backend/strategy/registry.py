"""Immutable strategy registry.

A version key is content-addressed by (key, parameter_hash). Changing parameters
requires a new key — rows are never rewritten in place.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from database.models.strategy import StrategyVersionRow
from database.session import session_factory
from strategy import StrategyPromotionStage

# Desk confirmation strategy — stamped onto every candidate / journal row.
# Same key for paper and live; broker environment is orthogonal.
LIVE_STRATEGY_NAME = "trader_desk"
LIVE_STRATEGY_TAG = "1.2.0"
LIVE_STRATEGY_KEY = f"{LIVE_STRATEGY_NAME}@{LIVE_STRATEGY_TAG}"

# Legacy confluence key kept registered so old journal rows still promote.
LEGACY_CONFLUENCE_KEY = "strategy_confluence@0.3.0-f3"

# Evaluation / walk-forward research stub (quant.backtesting.strategy.EmaTrendStub).
RESEARCH_STRATEGY_NAME = "ema_trend_stub"
RESEARCH_STRATEGY_TAG = "0.1.0"
RESEARCH_STRATEGY_KEY = f"{RESEARCH_STRATEGY_NAME}@{RESEARCH_STRATEGY_TAG}"

LIVE_PARAMETERS: dict[str, Any] = {
    "min_technical": 68,
    "min_overall": 70,
    "min_risk_reward": 2.0,
    "thesis": "bullish_multi_tf",
    "entry_model": "f3",
    "timeframes": ["1d", "4h", "1h", "15m"],
    "setups": [
        "pullback_continuation",
        "breakout_continuation",
        "gap_continuation",
    ],
    "broker_agnostic": True,
}

LEGACY_CONFLUENCE_PARAMETERS: dict[str, Any] = {
    "min_technical": 68,
    "min_overall": 70,
    "min_risk_reward": 2.0,
    "thesis": "bullish_confluence",
    "entry_model": "f3",
    "superseded_by": LIVE_STRATEGY_KEY,
}

RESEARCH_PARAMETERS: dict[str, Any] = {
    "ema_fast_default": 50,
    "ema_slow_default": 200,
    "grid": {"ema_fast": [20, 50], "ema_slow": [100, 200]},
}


def canonical_parameter_hash(parameters: dict[str, Any]) -> str:
    blob = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _row_to_dict(row: StrategyVersionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "key": row.key,
        "name": row.name,
        "version_tag": row.version_tag,
        "parameter_hash": row.parameter_hash,
        "parameters": row.parameters or {},
        "stage": row.stage,
        "evidence": row.evidence or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "approved_by": row.approved_by,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "rejected_reason": row.rejected_reason,
        "notes": row.notes,
    }


def get_by_key(key: str) -> dict[str, Any] | None:
    with session_factory()() as session:
        row = session.scalar(select(StrategyVersionRow).where(StrategyVersionRow.key == key))
        return _row_to_dict(row) if row else None


def get_by_id(version_id: UUID | str) -> dict[str, Any] | None:
    vid = UUID(str(version_id))
    with session_factory()() as session:
        row = session.get(StrategyVersionRow, vid)
        return _row_to_dict(row) if row else None


def list_versions() -> list[dict[str, Any]]:
    with session_factory()() as session:
        rows = session.scalars(
            select(StrategyVersionRow).order_by(StrategyVersionRow.created_at.desc())
        ).all()
        return [_row_to_dict(r) for r in rows]


def production_versions() -> list[dict[str, Any]]:
    with session_factory()() as session:
        rows = session.scalars(
            select(StrategyVersionRow).where(
                StrategyVersionRow.stage == StrategyPromotionStage.PRODUCTION.value
            )
        ).all()
        return [_row_to_dict(r) for r in rows]


def has_production_strategy() -> bool:
    return bool(production_versions())


def register_version(
    *,
    key: str,
    name: str,
    version_tag: str,
    parameters: dict[str, Any],
    notes: str | None = None,
) -> dict[str, Any]:
    """Idempotent register. Same key + same hash → existing row. Same key + new hash → error."""
    digest = canonical_parameter_hash(parameters)
    with session_factory()() as session:
        existing = session.scalar(select(StrategyVersionRow).where(StrategyVersionRow.key == key))
        if existing is not None:
            if existing.parameter_hash != digest:
                raise ValueError(
                    f"strategy version {key!r} is immutable: registered hash "
                    f"{existing.parameter_hash[:12]}… != {digest[:12]}…. "
                    "Bump the version tag and register a new key."
                )
            return _row_to_dict(existing)
        row = StrategyVersionRow(
            key=key,
            name=name,
            version_tag=version_tag,
            parameter_hash=digest,
            parameters=dict(parameters),
            stage=StrategyPromotionStage.PROPOSED.value,
            evidence={},
            notes=notes,
            updated_at=datetime.now(UTC),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def ensure_builtin_strategies() -> list[dict[str, Any]]:
    """Boot seed: desk strategy + research stub + legacy confluence key."""
    live = register_version(
        key=LIVE_STRATEGY_KEY,
        name=LIVE_STRATEGY_NAME,
        version_tag=LIVE_STRATEGY_TAG,
        parameters=LIVE_PARAMETERS,
        notes="Desk multi-TF path (paper and live share this version key).",
    )
    research = register_version(
        key=RESEARCH_STRATEGY_KEY,
        name=RESEARCH_STRATEGY_NAME,
        version_tag=RESEARCH_STRATEGY_TAG,
        parameters=RESEARCH_PARAMETERS,
        notes="EMA research stub for comparative Evaluation runs.",
    )
    legacy = register_version(
        key=LEGACY_CONFLUENCE_KEY,
        name="strategy_confluence",
        version_tag="0.3.0-f3",
        parameters=LEGACY_CONFLUENCE_PARAMETERS,
        notes="Superseded by trader_desk@1.2.0 — kept for journal continuity.",
    )
    return [live, research, legacy]


def current_live_strategy_key() -> str:
    """Key stamped onto new TradeCandidates — broker-env agnostic."""
    ensure_builtin_strategies()
    return LIVE_STRATEGY_KEY
