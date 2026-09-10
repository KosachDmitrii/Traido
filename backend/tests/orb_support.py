"""Explicit synthetic ORB vendor inputs for existing execution lifecycle tests.

Builds real plans through the production policy; does not stub admission or risk.
"""

from datetime import datetime, time
from decimal import Decimal

from core.clock import ET
from core.enums import EntryDecision, SetupType, Timeframe
from core.schemas import Bar, TradeCandidate
from strategy.orb import VERSION, _previous_sessions, form_plan
from strategy.orb.store import read_session


def orb_ready_candidate(candidate: TradeCandidate, *, feed: str = "sip") -> TradeCandidate:
    if candidate.strategy_version == VERSION:
        return candidate
    from database.models.orb import OrbSessionRow
    from database.session import session_factory
    from trading.execution import _utcnow

    now = _utcnow()
    # A pre-existing proposal can be approved outside RTH; form it on the last regular test session.
    from trading.session_hours import us_equity_rth_open

    if not us_equity_rth_open(now):
        from tests.conftest import RTH_INSTANT

        now = RTH_INSTANT
    entry = candidate.entry
    distance = max(Decimal("0.02"), entry * Decimal("0.0002"))
    high = entry - distance - Decimal("0.01")
    trigger = high + Decimal("0.01")
    atr = (trigger - candidate.stop) * 10
    daily = []
    for day in _previous_sessions(now, 15):
        daily.append(
            Bar(
                symbol=candidate.symbol,
                timeframe=Timeframe.D1,
                ts=datetime.combine(day, time(0), ET),
                open=entry,
                close=entry,
                high=entry + atr / 2,
                low=entry - atr / 2,
                volume=Decimal(5000000),
                source="synthetic_orb",
            )
        )
    opening = []
    for day in [*_previous_sessions(now, 14), now.astimezone(ET).date()]:
        opening.append(
            Bar(
                symbol=candidate.symbol,
                timeframe=Timeframe.M5,
                ts=datetime.combine(day, time(9, 30), ET),
                open=high - Decimal("0.2"),
                close=high - Decimal("0.1"),
                high=high,
                low=high - Decimal("0.4"),
                volume=Decimal(100000),
                source="synthetic_orb",
            )
        )
    decision = form_plan(candidate.symbol, daily, opening, now=now, feed=feed)
    assert decision.plan is not None, decision.reasons
    plan = decision.plan
    with session_factory()() as db:
        row = db.get(OrbSessionRow, plan.session)
        payload = (
            dict(row.payload)
            if row
            else {"plans": {}, "states": {}, "session": plan.session, "version": VERSION}
        )
        payload["plans"] = {**payload["plans"], plan.symbol: plan.model_dump(mode="json")}
        if row:
            row.payload = payload
        else:
            db.add(OrbSessionRow(session=plan.session, payload=payload))
        db.commit()
    return TradeCandidate(
        symbol=candidate.symbol,
        action=candidate.action,
        confidence=0,
        entry=entry,
        stop=plan.stop,
        exit_policy="session_close",
        exit_at=plan.exit_at,
        orb_plan=plan.model_dump(mode="json"),
        reasons=["synthetic ORB execution fixture"],
        strategy_version=VERSION,
        exec_timeframe=Timeframe.M5,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        entry_decision=EntryDecision.BUY_NOW,
        admission_version=VERSION,
        policy_version=VERSION,
        pipeline_run_id=candidate.pipeline_run_id,
    )


def opening_bar(symbol: str) -> list[Bar]:
    from trading.execution import _utcnow

    selected = read_session(str(_utcnow().astimezone(ET).date()))
    plan = (selected or {}).get("plans", {}).get(symbol)
    return [Bar.model_validate(plan["evidence"]["opening"][-1])] if plan else []
