"""Promotion gate — evidence in, stage out. Humans approve; agents do not."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select

from database.models.journal import BacktestRunRow, TradeJournalRow
from database.models.strategy import StrategyEvaluationRunRow, StrategyVersionRow
from database.session import session_factory
from strategy import (
    PROMOTION_ORDER,
    StrategyPromotionStage,
    stage_at_least,
    stage_index,
)
from strategy.thresholds import PromotionThresholds, get_promotion_thresholds


class PromotionError(ValueError):
    """Illegal promotion transition or missing evidence."""


def _paper_stats(session, key: str) -> dict[str, Any]:
    rows = session.scalars(
        select(TradeJournalRow).where(
            TradeJournalRow.strategy_version == key,
            TradeJournalRow.backtest_run_id.is_(None),
        )
    ).all()
    if not rows:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "expectancy_usd": None,
            "profit_factor": None,
            "regimes": {},
            "pass": False,
            "reasons": ["no_paper_trades"],
        }
    pnls = [Decimal(str(r.pnl)) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins, Decimal(0))
    gross_loss = abs(sum(losses, Decimal(0)))
    expectancy = float(sum(pnls) / len(pnls))
    pf = float(gross_win / gross_loss) if gross_loss > 0 else None
    regimes: dict[str, int] = {}
    for r in rows:
        label = (r.market_regime or "unknown").strip() or "unknown"
        regimes[label] = regimes.get(label, 0) + 1
    return {
        "trade_count": len(rows),
        "win_rate": len(wins) / len(rows),
        "expectancy_usd": expectancy,
        "profit_factor": pf,
        "regimes": regimes,
        "pass": False,
        "reasons": [],
    }


def _backtest_stats(session, key: str) -> dict[str, Any]:
    rows = session.scalars(
        select(BacktestRunRow)
        .where(BacktestRunRow.strategy_version == key)
        .order_by(BacktestRunRow.created_at.desc())
    ).all()
    if not rows:
        return {
            "trade_count": 0,
            "return_pct": None,
            "runs": 0,
            "pass": False,
            "reasons": ["no_backtest_runs"],
        }
    best = max(rows, key=lambda r: (r.return_pct, r.trade_count))
    return {
        "trade_count": best.trade_count,
        "return_pct": best.return_pct,
        "profit_factor": best.profit_factor,
        "max_drawdown_pct": best.max_drawdown_pct,
        "runs": len(rows),
        "symbol": best.symbol,
        "pass": False,
        "reasons": [],
    }


def _oos_aggregate(session, key: str) -> dict[str, Any]:
    runs = session.scalars(
        select(StrategyEvaluationRunRow)
        .where(StrategyEvaluationRunRow.strategy_version_key == key)
        .order_by(StrategyEvaluationRunRow.generated_at.desc())
    ).all()
    if not runs:
        return {
            "samples": 0,
            "pass_count": 0,
            "fail_count": 0,
            "latest_verdict": None,
            "oos_trades": 0,
            "oos_return_pct": None,
            "profit_factor": None,
            "walk_forward_efficiency": None,
            "regimes_with_trades": 0,
            "pass_oos": False,
            "pass_wfe": False,
            "reasons": ["no_evaluation_runs"],
        }
    latest = runs[0]
    payload = latest.payload or {}
    oos = payload.get("out_of_sample") or payload.get("oos") or {}
    regimes = payload.get("by_regime") or []
    regimes_with = sum(1 for r in regimes if int(r.get("trade_count") or 0) > 0)
    pass_count = sum(1 for r in runs if str(r.verdict).startswith("PASS"))
    return {
        "samples": len(runs),
        "pass_count": pass_count,
        "fail_count": len(runs) - pass_count,
        "latest_verdict": latest.verdict,
        "latest_symbol": latest.symbol,
        "oos_trades": int(oos.get("trade_count") or payload.get("oos_trade_count") or 0),
        "oos_return_pct": oos.get("return_pct", payload.get("oos_return_pct")),
        "profit_factor": oos.get("profit_factor", payload.get("oos_profit_factor")),
        "walk_forward_efficiency": oos.get(
            "walk_forward_efficiency", payload.get("walk_forward_efficiency")
        ),
        "regimes_with_trades": regimes_with,
        "pass_oos": False,
        "pass_wfe": False,
        "reasons": [],
    }


def _apply_thresholds(
    *,
    backtest: dict[str, Any],
    oos: dict[str, Any],
    paper: dict[str, Any],
    thr: PromotionThresholds,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    bt_reasons: list[str] = []
    if backtest["trade_count"] < thr.min_backtest_trades:
        bt_reasons.append("min_backtest_trades")
    ret = backtest.get("return_pct")
    if ret is None or float(ret) <= thr.min_backtest_return_pct:
        bt_reasons.append("min_backtest_return_pct")
    backtest = {
        **backtest,
        "pass": not bt_reasons and backtest["trade_count"] > 0,
        "reasons": bt_reasons,
    }

    oos_reasons: list[str] = []
    if oos["oos_trades"] < thr.min_oos_trades:
        oos_reasons.append("min_oos_trades")
    oos_ret = oos.get("oos_return_pct")
    if oos_ret is None or float(oos_ret) <= thr.min_oos_return_pct:
        oos_reasons.append("min_oos_return_pct")
    pf = oos.get("profit_factor")
    if pf is None or float(pf) < thr.min_profit_factor:
        oos_reasons.append("min_profit_factor")
    if oos["regimes_with_trades"] and oos["regimes_with_trades"] < thr.min_regimes_with_trades:
        oos_reasons.append("min_regimes_with_trades")
    oos_pass = not oos_reasons and oos["oos_trades"] > 0

    wfe = oos.get("walk_forward_efficiency")
    wfe_ok = wfe is not None and float(wfe) >= thr.min_walk_forward_efficiency
    wfe_reasons = [] if wfe_ok else ["min_walk_forward_efficiency"]
    oos = {
        **oos,
        "pass_oos": oos_pass,
        "pass_wfe": wfe_ok,
        "reasons": oos_reasons + (wfe_reasons if not wfe_ok else []),
    }

    paper_reasons: list[str] = []
    if paper["trade_count"] < thr.min_paper_trades:
        paper_reasons.append("min_paper_trades")
    exp = paper.get("expectancy_usd")
    if exp is None or float(exp) <= thr.min_paper_expectancy_usd:
        paper_reasons.append("min_paper_expectancy_usd")
    ppf = paper.get("profit_factor")
    if ppf is not None and float(ppf) < thr.min_paper_profit_factor:
        paper_reasons.append("min_paper_profit_factor")
    regime_n = len([k for k, v in (paper.get("regimes") or {}).items() if v > 0 and k != "unknown"])
    # Only enforce multi-regime when tags exist beyond unknown.
    if regime_n >= 1 and regime_n < thr.min_regimes_with_trades:
        paper_reasons.append("min_regimes_with_trades")
    paper = {
        **paper,
        "pass": not paper_reasons and paper["trade_count"] > 0,
        "reasons": paper_reasons,
    }
    return backtest, oos, paper


def _earned_research_stage(
    backtest: dict[str, Any],
    oos: dict[str, Any],
    paper: dict[str, Any],
) -> StrategyPromotionStage:
    if not backtest.get("pass"):
        return StrategyPromotionStage.PROPOSED
    if not oos.get("pass_oos"):
        return StrategyPromotionStage.BACKTEST_PASSED
    if not oos.get("pass_wfe"):
        return StrategyPromotionStage.OOS_PASSED
    if not paper.get("pass"):
        return StrategyPromotionStage.WALK_FORWARD_PASSED
    return StrategyPromotionStage.PAPER_PASSED


def recompute_version(version_id: UUID | str) -> dict[str, Any]:
    """Refresh evidence and advance stage up to PAPER_PASSED (never past human)."""
    thr = get_promotion_thresholds()
    vid = UUID(str(version_id))
    with session_factory()() as session:
        row = session.get(StrategyVersionRow, vid)
        if row is None:
            raise PromotionError("strategy version not found")
        if row.stage == StrategyPromotionStage.REJECTED.value:
            raise PromotionError("rejected versions cannot be recomputed")

        backtest = _backtest_stats(session, row.key)
        oos = _oos_aggregate(session, row.key)
        paper = _paper_stats(session, row.key)
        backtest, oos, paper = _apply_thresholds(backtest=backtest, oos=oos, paper=paper, thr=thr)
        earned = _earned_research_stage(backtest, oos, paper)

        evidence = dict(row.evidence or {})
        evidence["thresholds"] = thr.as_dict()
        evidence["backtest"] = backtest
        evidence["out_of_sample"] = oos
        evidence["paper"] = paper
        evidence["earned_stage"] = earned.value
        evidence["recomputed_at"] = datetime.now(UTC).isoformat()

        current = StrategyPromotionStage(row.stage)
        # Never auto-demote past human/production; never auto-promote into them.
        if current in {
            StrategyPromotionStage.HUMAN_APPROVED,
            StrategyPromotionStage.PRODUCTION,
        }:
            # Still refresh evidence; keep stage.
            new_stage = current
        elif stage_index(earned) > stage_index(current):
            new_stage = earned
        elif stage_index(earned) < stage_index(current) and current not in {
            StrategyPromotionStage.HUMAN_APPROVED,
            StrategyPromotionStage.PRODUCTION,
        }:
            # Demote research stages when evidence no longer supports them.
            new_stage = earned
        else:
            new_stage = current

        # Cap auto stage at PAPER_PASSED.
        if stage_index(new_stage) > stage_index(StrategyPromotionStage.PAPER_PASSED):
            new_stage = StrategyPromotionStage.PAPER_PASSED
            if current in {
                StrategyPromotionStage.HUMAN_APPROVED,
                StrategyPromotionStage.PRODUCTION,
            }:
                new_stage = current

        row.evidence = evidence
        row.stage = new_stage.value
        row.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(row)
        from strategy.registry import _row_to_dict

        return _row_to_dict(row)


def human_approve(version_id: UUID | str, *, actor: str) -> dict[str, Any]:
    """Explicit human gate. Requires PAPER_PASSED evidence still holding."""
    # Refresh first so approve cannot ignore stale fails.
    payload = recompute_version(version_id)
    if payload["stage"] == StrategyPromotionStage.REJECTED.value:
        raise PromotionError("version is rejected")
    if not stage_at_least(payload["stage"], StrategyPromotionStage.PAPER_PASSED):
        raise PromotionError(
            f"paper gate not passed (stage={payload['stage']}); "
            "recompute and satisfy paper thresholds before approve"
        )
    if payload["stage"] in {
        StrategyPromotionStage.HUMAN_APPROVED.value,
        StrategyPromotionStage.PRODUCTION.value,
    }:
        return payload

    vid = UUID(str(version_id))
    with session_factory()() as session:
        row = session.get(StrategyVersionRow, vid)
        assert row is not None
        row.stage = StrategyPromotionStage.HUMAN_APPROVED.value
        row.approved_at = datetime.now(UTC)
        row.approved_by = actor[:128]
        row.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(row)
        from strategy.registry import _row_to_dict

        return _row_to_dict(row)


def promote_to_production(version_id: UUID | str, *, actor: str) -> dict[str, Any]:
    """HUMAN_APPROVED → PRODUCTION. Live/autopilot may only use PRODUCTION."""
    vid = UUID(str(version_id))
    with session_factory()() as session:
        row = session.get(StrategyVersionRow, vid)
        if row is None:
            raise PromotionError("strategy version not found")
        if row.stage == StrategyPromotionStage.PRODUCTION.value:
            from strategy.registry import _row_to_dict

            return _row_to_dict(row)
        if row.stage != StrategyPromotionStage.HUMAN_APPROVED.value:
            raise PromotionError(f"production requires human_approved (stage={row.stage})")
        row.stage = StrategyPromotionStage.PRODUCTION.value
        row.updated_at = datetime.now(UTC)
        evidence = dict(row.evidence or {})
        evidence["production"] = {
            "promoted_at": datetime.now(UTC).isoformat(),
            "promoted_by": actor[:128],
        }
        row.evidence = evidence
        session.commit()
        session.refresh(row)
        from strategy.registry import _row_to_dict

        return _row_to_dict(row)


def reject_version(version_id: UUID | str, *, actor: str, reason: str) -> dict[str, Any]:
    vid = UUID(str(version_id))
    with session_factory()() as session:
        row = session.get(StrategyVersionRow, vid)
        if row is None:
            raise PromotionError("strategy version not found")
        if row.stage == StrategyPromotionStage.PRODUCTION.value:
            raise PromotionError(
                "production versions cannot be rejected in place; register a successor"
            )
        row.stage = StrategyPromotionStage.REJECTED.value
        row.rejected_at = datetime.now(UTC)
        row.rejected_reason = f"{actor}: {reason}"[:2000]
        row.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(row)
        from strategy.registry import _row_to_dict

        return _row_to_dict(row)


def persist_evaluation_run(
    *,
    strategy_version_key: str,
    symbol: str,
    timeframe: str,
    verdict: str,
    payload: dict[str, Any],
    generated_at: datetime | None = None,
) -> None:
    with session_factory()() as session:
        session.add(
            StrategyEvaluationRunRow(
                strategy_version_key=strategy_version_key,
                symbol=symbol.upper(),
                timeframe=timeframe,
                generated_at=generated_at or datetime.now(UTC),
                verdict=verdict,
                payload=payload,
            )
        )
        session.commit()


def require_production_strategy(version_key: str | None = None) -> None:
    """Gate for live / autopilot. Paper confirmation does not call this."""
    from strategy.registry import get_by_key, has_production_strategy

    if version_key:
        row = get_by_key(version_key)
        if row is None or row["stage"] != StrategyPromotionStage.PRODUCTION.value:
            raise PromotionError(
                f"strategy {version_key!r} is not in PRODUCTION; live/autopilot refused"
            )
        return
    if not has_production_strategy():
        raise PromotionError("no STRATEGY version in PRODUCTION; live/autopilot refused")
