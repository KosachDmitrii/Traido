"""Read-only replay of persisted ORB evidence, without assumed quotes or fills."""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.schemas import Bar
from database.models.orb import OrbDecisionEventRow, OrbSessionRow
from database.session import session_factory
from market_data.bar_store import load_bars_many
from strategy.orb import OrbPlan
from strategy.orb.retest import rebuild


def replay_plan(plan: OrbPlan, bars: list[Bar], *, as_of: datetime) -> dict[str, Any]:
    """Evaluate each completed bar using its available prefix only.

    Durable bars do not prove provider coverage of missing intervals. Gaps
    remain blocked. A confirmed setup is not a fresh-quote admission or a fill.
    """
    rows = sorted(
        (
            b
            for b in bars
            if plan.range_end <= b.ts
            and b.ts + timedelta(minutes=5) <= as_of
            and b.ts + timedelta(minutes=5) < plan.entry_deadline
        ),
        key=lambda b: b.ts,
    )
    reasons: Counter[str] = Counter()
    signals: dict[str, dict[str, Any]] = {}
    for i, bar in enumerate(rows):
        evaluated_at = bar.ts + timedelta(minutes=5)
        if evaluated_at >= plan.entry_deadline:
            break
        decision = rebuild(plan, rows[: i + 1], now=evaluated_at)
        reasons.update(decision.reasons)
        retest = decision.plan.evidence.get("retest") if decision.plan else None
        if retest:
            signals.setdefault(
                retest["confirmed_at"],
                {
                    "confirmed_at": retest["confirmed_at"],
                    "valid_until": retest["valid_until"],
                    "entry_min": retest["entry_min"],
                    "max_entry": str(decision.plan.max_entry),
                    "stop": str(decision.plan.stop),
                    "target": retest["target"],
                },
            )
    expected = plan.range_end
    gaps = 0
    for bar in rows:
        if bar.ts > expected:
            gaps += int((bar.ts - expected).total_seconds() // 300)
        expected = max(expected, bar.ts + timedelta(minutes=5))
    required_end = min(as_of, plan.entry_deadline)
    required_end = required_end.replace(
        minute=required_end.minute - required_end.minute % 5, second=0, microsecond=0
    )
    missing_tail = max(0, int((required_end - expected).total_seconds() // 300))
    return {
        "symbol": plan.symbol,
        "version": plan.version,
        "bars": len(rows),
        "missing_intervals": gaps + missing_tail,
        "reason_evaluations": dict(reasons),
        "signals": list(signals.values()),
    }


def session_replay(
    day: date, *, as_of: datetime | None = None, replay_bars: bool = True
) -> dict[str, Any]:
    """Inspect one saved selection and real M5 evidence; never fetch or write."""
    as_of = as_of or datetime.now(UTC)
    with session_factory()() as db:
        saved = db.get(OrbSessionRow, day.isoformat())
        if saved is None:
            return {"session": day.isoformat(), "status": "not_recorded"}
        payload = saved.payload
        # Query aggregates instead of loading the potentially large event corpus.
        from sqlalchemy import func

        observations = db.execute(
            select(OrbDecisionEventRow.to_state, OrbDecisionEventRow.reason_codes, func.count())
            .where(OrbDecisionEventRow.session == day.isoformat())
            .group_by(OrbDecisionEventRow.to_state, OrbDecisionEventRow.reason_codes)
        ).all()
    observation_states: Counter[str] = Counter()
    observation_reasons: Counter[str] = Counter()
    for state, codes, count in observations:
        observation_states[state] += count
        for code in codes:
            observation_reasons[code] += count
    recorded = {
        "session": day.isoformat(),
        "recorded_observation_states": dict(observation_states),
        "recorded_observation_reasons": dict(observation_reasons),
    }
    if not replay_bars:
        return {**recorded, "status": "recorded_observations"}
    plans = [OrbPlan.model_validate(raw) for raw in payload.get("plans", {}).values()]
    histories: dict[str, list[Bar]] = {}
    groups: dict[tuple[str, datetime], list[OrbPlan]] = {}
    for plan in plans:
        groups.setdefault((plan.source, plan.range_end), []).append(plan)
    for (source, start), members in groups.items():
        histories.update(
            load_bars_many(
                source, [p.symbol for p in members], start, min(as_of, members[0].exit_at)
            )
        )
    results = [replay_plan(p, histories.get(p.symbol, []), as_of=as_of) for p in plans]
    reasons: Counter[str] = Counter()
    for result in results:
        reasons.update(result["reason_evaluations"])
    return {
        **recorded,
        "status": "replayed",
        "scope": "saved_selected_plans_only",
        "quote_admission_replayed": False,
        "fills_simulated": False,
        "counts": payload.get("counts", {}),
        "selection_rejections": payload.get("rejection_counts", {}),
        "plans": len(plans),
        "plans_with_bars": sum(bool(r["bars"]) for r in results),
        "plans_with_missing_intervals": sum(bool(r["missing_intervals"]) for r in results),
        "confirmed_setups": sum(len(r["signals"]) for r in results),
        "symbols_with_confirmed_setups": sum(bool(r["signals"]) for r in results),
        "reason_evaluations": dict(reasons),
        "symbols": results,
    }
