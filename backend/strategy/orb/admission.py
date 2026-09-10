"""ORB admission uses immutable range evidence, not legacy arrival scores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from core.ports import MarketDataPort
from core.schemas import MarketAssessment, Quote, TradeCandidate

if TYPE_CHECKING:
    from trading.final_admission import FinalAdmissionEvaluation

from core.enums import AdmissionDecision, DataHealthStatus, SetupType, Timeframe
from core.schemas import AdmissionInput, TradeAdmissionResult
from strategy.orb import SUPPORTED_VERSIONS, OrbPlan, evaluate_trigger, form_plan


def evaluate_sealed(inp: AdmissionInput) -> TradeAdmissionResult:
    reasons = []
    try:
        plan = OrbPlan.model_validate(inp.orb_plan)
        if inp.strategy_version != plan.version or inp.stop_price != plan.stop:
            reasons.append("ORB_GEOMETRY_CHANGED")
        decision = evaluate_trigger(
            plan, inp.quote, now=inp.evaluated_at, limit_price=inp.limit_price
        )
        if decision.state != "BUY_ALLOWED":
            reasons.extend(decision.reasons)
    except (ValueError, TypeError):
        reasons.append("ORB_EVIDENCE_INVALID")
    return TradeAdmissionResult(
        decision=AdmissionDecision.NO_TRADE if reasons else AdmissionDecision.BUY_ALLOWED,
        admitted=not reasons,
        buy_ready=not reasons,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        reason_codes=reasons or ["ORB_BREAKOUT_CONFIRMED"],
        admission_version=inp.strategy_version,
    )


async def final_admission(
    candidate: TradeCandidate,
    *,
    quote: Quote,
    market_data: MarketDataPort,
    now: datetime | None = None,
    market: MarketAssessment | None = None,
    sector_label: str | None = None,
    sector_tradable: bool | None = None,
    sector_benchmark: str | None = None,
    sector_provider: str | None = None,
    sector_source_ts: datetime | None = None,
    require_sector: bool = True,
    opportunity_id: UUID | None = None,
    decision_version: int = 0,
) -> FinalAdmissionEvaluation:
    from trading.final_admission import FinalAdmissionEvaluation
    from trading.final_pretrade import PretradeRejection
    from trading.geometry_hash import geometry_hash_from_candidate
    from trading.market_gate import evaluate_market_gate

    now = now or datetime.now(UTC)
    if (
        candidate.strategy_version not in SUPPORTED_VERSIONS
        or candidate.exit_policy != "session_close"
    ):
        raise PretradeRejection("STRATEGY_RETIRED", "ORB_REQUIRED")
    feed = getattr(market_data, "_feed", None)
    if feed not in {"iex", "sip"}:
        raise PretradeRejection("DATA_BLOCKED", "ORB_UNSUPPORTED_FEED")
    plan = OrbPlan.model_validate(candidate.orb_plan)
    if plan.source != f"alpaca:{feed}":
        raise PretradeRejection("DATA_BLOCKED", "ORB_DATA_FEED_MISMATCH")
    from core.schemas import Bar
    from strategy.orb.store import read_session

    selected = read_session(plan.session)
    if selected is None or selected.get("plans", {}).get(plan.symbol) != plan.model_dump(
        mode="json"
    ):
        raise PretradeRejection("ORB_NOT_SELECTED", candidate.symbol)
    if candidate.symbol != plan.symbol or candidate.strategy_version != plan.version:
        raise PretradeRejection("ORB_GEOMETRY_CHANGED", "symbol")
    # Re-read today's range. Historical inputs are immutable and were captured at selection.
    today = await market_data.get_bars(
        candidate.symbol,
        Timeframe.M5,
        plan.range_start.astimezone(UTC),
        plan.range_end.astimezone(UTC) - timedelta(microseconds=1),
    )
    daily = [Bar.model_validate(b) for b in plan.evidence.get("daily", [])]
    opening = [Bar.model_validate(b) for b in plan.evidence.get("opening", [])][:-1] + today
    rebuilt = form_plan(candidate.symbol, daily, opening, now=now, feed=feed, version=plan.version)
    if rebuilt.plan is None:
        raise PretradeRejection("ORB_INVALIDATED", ",".join(rebuilt.reasons))
    fresh = rebuilt.plan
    for key in ("trigger", "stop", "max_entry", "session", "entry_deadline", "exit_at"):
        if getattr(plan, key) != getattr(fresh, key):
            raise PretradeRejection("ORB_GEOMETRY_CHANGED", key)
    if (
        candidate.stop != plan.stop
        or candidate.exit_at != plan.exit_at
        or candidate.target is not None
    ):
        raise PretradeRejection("ORB_GEOMETRY_CHANGED", "candidate")
    gate = evaluate_market_gate(
        market,
        now=now,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
        require_sector=require_sector,
    )
    if not gate.tradable_long or gate.status is DataHealthStatus.UNHEALTHY:
        raise PretradeRejection("MARKET_GATE_REJECTED", ",".join(gate.reason_codes))
    gh = geometry_hash_from_candidate(candidate)
    inp = AdmissionInput(
        orb_plan=plan.model_dump(mode="json"),
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        setup_quality=0,
        quote=quote,
        bars_count=len(opening),
        bar_timeframe="5m",
        last_bar_ts=max(b.ts for b in opening),
        market=market,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
        sector_benchmark=sector_benchmark,
        sector_provider=sector_provider,
        sector_source_ts=sector_source_ts,
        strategy_version=plan.version,
        policy_version=plan.version,
        admission_version=plan.version,
        aggressiveness=0,
        opportunity_id=opportunity_id,
        decision_version=decision_version,
        geometry_hash=gh,
        evaluated_at=now,
        limit_price=candidate.entry,
        stop_price=candidate.stop,
    )
    adm = evaluate_sealed(inp)
    if not adm.admitted:
        raise PretradeRejection("ORB_ENTRY_REJECTED", ",".join(adm.reason_codes))
    return FinalAdmissionEvaluation(
        admission=adm,
        admission_input=inp,
        quote=quote,
        snapshot=None,
        market_gate=gate,
        bars_count=len(opening),
        evaluated_at=now,
        last_bar_ts=inp.last_bar_ts,
        geometry_hash=gh,
    )
