"""One persisted opening-range selection per session, observed independently of risk."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from threading import Lock
from typing import Any, cast
from uuid import uuid4

from core.activity import BOARD
from core.clock import ET
from core.config import get_settings
from core.enums import (
    EntryDecision,
    OpportunityStatus,
    RiskVerdict,
    SetupType,
    Timeframe,
    TradeAction,
)
from core.schemas import Bar, PipelineResult, TradeCandidate
from strategy.orb import (
    PARAMETERS,
    SUPPORTED_VERSIONS,
    VERSION,
    OrbPlan,
    _previous_sessions,
    _valid_bar,
    evaluate_trigger,
    form_plan,
)
from strategy.orb.data_access import data_error_reason
from strategy.orb.store import create_session, read_session, update_state, update_states
from trading.scan_context import ScanContext, open_scan_context
from trading.session_hours import us_equity_rth_open
from universe.models import UniverseTier
from universe.service import UniverseService

_discovery_lock = asyncio.Lock()
_observation_task: asyncio.Task[dict[str, int]] | None = None
_priority_lock = asyncio.Lock()
_pending_completed_bars: dict[tuple[str, datetime], Bar] = {}
_pending_completed_bars_lock = Lock()
_last_ready_check = 0.0
STATUS: dict[str, Any] = {"status": "not_started", "version": VERSION, "parameters": PARAMETERS}


def notify_completed_bar(bar: Bar) -> None:
    """Queue a completed SIP bar for the lightweight observation path.

    Stream ingestion runs in a worker thread, so this deliberately uses a tiny
    locked process-local dictionary instead of asyncio primitives. Duplicate
    vendor corrections collapse by key.
    """
    with _pending_completed_bars_lock:
        _pending_completed_bars[(bar.symbol.upper(), bar.ts)] = bar


def _bar_observation(bar: Bar, *, observed_at: datetime) -> dict[str, Any]:
    closes_at = bar.ts + timedelta(minutes=5)
    boundary = observed_at.replace(second=0, microsecond=0)
    boundary = boundary.replace(minute=boundary.minute - boundary.minute % 5)
    return {
        "observed_at": observed_at.isoformat(),
        "last_bar": bar.model_dump(mode="json"),
        "last_bar_closes_at": closes_at.isoformat(),
        "processing_lag_seconds": max(0, round((observed_at - closes_at).total_seconds(), 3)),
        "next_bar_closes_at": (boundary + timedelta(minutes=5)).isoformat(),
    }


def _restore_session_board(session: dict[str, Any]) -> None:
    """Project a persisted ORB session onto the operator activity board."""
    counts = session.get("counts") or {}
    selected = len(session.get("plans") or {})
    BOARD.set_agent(
        "universe",
        status="done",
        detail=f"SIP universe · {counts.get('eligible', 0)} eligible",
    )
    BOARD.set_agent(
        "structure",
        status="done",
        detail=f"Opening ranges ready · {selected} selected",
    )
    BOARD.set_agent(
        "risk_plan",
        status="done",
        detail=f"ORB geometry stored · {selected} plans",
    )
    for agent_id, detail in (
        ("context", "Waiting for a confirmed ORB entry"),
        ("checklist", "Waiting for final admission"),
        ("risk", "Waiting for final admission"),
    ):
        current = next(item for item in BOARD.snapshot()["agents"] if item["id"] == agent_id)
        if current["updated_at"] is None:
            BOARD.set_agent(agent_id, status="idle", detail=detail)


async def discover(
    ctx: ScanContext, universe: UniverseService, *, now: datetime | None = None
) -> dict[str, Any]:
    """Keep all qualifying plans, ordered by opening relative volume."""
    now = now or datetime.now(UTC)
    day = str(now.astimezone(ET).date())
    async with _discovery_lock:
        feed_name = getattr(ctx.market_data, "_feed", None)
        existing = await asyncio.to_thread(read_session, day)
        if existing is not None:
            STATUS.clear()
            STATUS.update(existing)
            saved_feed = existing.get("feed", existing.get("parameters", {}).get("feed"))
            if existing.get("version") not in SUPPORTED_VERSIONS or saved_feed != feed_name:
                STATUS.update(status="data_blocked", reason="ORB_SESSION_CONFIGURATION_CHANGED")
                return dict(STATUS)
            # This rollout is Paper-only and cannot modify a published plan.
            from core.config import get_settings
            from core.enums import BrokerEnvironment
            from strategy.orb.store import upgrade_unpublished_entry_limits

            if get_settings().broker_env is BrokerEnvironment.PAPER:
                existing = (
                    await asyncio.to_thread(upgrade_unpublished_entry_limits, day, now=now)
                ) or existing
                STATUS.update(existing)
            if existing.get("selection_scope") == "all_qualified":
                _restore_session_board(existing)
                return existing
        STATUS.clear()
        STATUS.update(
            version=VERSION,
            feed=feed_name,
            parameters=PARAMETERS,
            status="forming_range",
            session=day,
            plans={},
            states={},
            counts={},
        )
        start = datetime.combine(now.astimezone(ET).date(), time(9, 30), ET)
        if not us_equity_rth_open(now) or now < start + timedelta(minutes=5):
            STATUS["reason"] = (
                "ORB_OPENING_RANGE_FORMING"
                if us_equity_rth_open(now)
                else "ORB_OUTSIDE_ENTRY_SESSION"
            )
            return dict(STATUS)
        feed = ctx.market_data
        if feed_name != "sip" or not callable(getattr(feed, "get_bars_batch", None)):
            STATUS.update(status="data_blocked", reason="ORB_UNSUPPORTED_FEED")
            return dict(STATUS)
        STATUS.update(status="loading_history", reason=None)
        BOARD.set_agent("universe", status="working", detail="Loading Alpaca SIP universe")
        snapshot = await universe.get_scan_universe(tier=UniverseTier.BROAD, max_size=0)
        symbols = snapshot.symbols
        BOARD.set_agent(
            "universe",
            status="done",
            detail=f"SIP universe · {len(symbols)} eligible",
        )
        counts = {
            "universe": snapshot.total,
            "eligible": len(symbols),
            "history_missing": 0,
            "base_rejected": 0,
            "opening_evaluated": 0,
            "qualified": 0,
            "selected": 0,
        }
        STATUS["counts"] = counts
        try:
            daily = await ctx.daily_bars(symbols, now - timedelta(days=45), start)
        except Exception as exc:
            STATUS.update(status="data_blocked", reason=data_error_reason(exc, feed=feed_name))
            raise
        days = _previous_sessions(now, 15)
        base: list[str] = []
        rejected: dict[str, list[str]] = {}
        for symbol in symbols:
            bars = daily.get(symbol, [])
            prior = {b.ts.astimezone(ET).date(): b for b in bars if _valid_bar(b)}
            if any(d not in prior for d in days):
                counts["history_missing"] += 1
                rejected[symbol] = ["ORB_HISTORY_INCOMPLETE"]
                continue
            bs = [prior[d] for d in days]
            adv = sum((b.volume for b in bs[-14:]), Decimal(0)) / 14
            atr = (
                sum(
                    (
                        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
                        for a, b in pairwise(bs)
                    ),
                    Decimal(0),
                )
                / 14
            )
            volume_low = adv < 1000000
            if volume_low or atr <= Decimal("0.50"):
                counts["base_rejected"] += 1
                rejected[symbol] = (["ORB_DAILY_VOLUME_LOW"] if volume_low else []) + (
                    ["ORB_ATR_LOW"] if atr <= Decimal("0.50") else []
                )
            else:
                base.append(symbol)
        opening: dict[str, list[Bar]] = {s: [] for s in base}
        # Only 15 five-minute windows, not 45 days of intraday data for the universe.
        STATUS["status"] = "loading_opening_ranges"
        BOARD.set_agent(
            "structure",
            status="working",
            detail="Building 09:30–09:35 ET opening ranges",
        )
        batch = cast(Any, feed).get_bars_batch
        for d in [*days[-14:], now.astimezone(ET).date()]:
            t = datetime.combine(d, time(9, 30), ET).astimezone(UTC)
            try:
                rows = await batch(
                    base, t, t + timedelta(minutes=5) - timedelta(microseconds=1), Timeframe.M5
                )
            except Exception as exc:
                STATUS.update(status="data_blocked", reason=data_error_reason(exc, feed=feed_name))
                raise
            for symbol in base:
                opening[symbol].extend(rows.get(symbol, []))
        BOARD.set_agent(
            "structure",
            status="done",
            detail=f"Opening ranges loaded · {len(base)} instruments",
        )
        BOARD.set_agent("risk_plan", status="working", detail="Building ORB trade geometry")
        plans: list[OrbPlan] = []
        for symbol in base:
            decision = form_plan(
                symbol, daily.get(symbol, []), opening[symbol], now=now, feed=feed_name
            )
            counts["opening_evaluated"] += 1
            if decision.plan is not None:
                plans.append(decision.plan)
            else:
                rejected[symbol] = decision.reasons
        plans.sort(key=lambda p: (-p.relative_volume, p.symbol))
        selected = plans
        BOARD.set_agent(
            "risk_plan",
            status="done",
            detail=f"ORB geometry stored · {len(selected)} plans",
        )
        counts.update(qualified=len(plans), selected=len(selected))
        payload = {
            "version": VERSION,
            "feed": feed_name,
            "parameters": PARAMETERS,
            "session": day,
            "status": "ready",
            "entry_policy_rollout": PARAMETERS["entry_policy_revision"],
            "entry_policy_changes": [],
            "created_at": now.isoformat(),
            "counts": counts,
            "reason": None,
            "plans": {p.symbol: p.model_dump(mode="json") for p in selected},
            "states": {
                p.symbol: {"state": "WAIT", "reasons": ["ORB_RETEST_WAIT_BREAKOUT"]}
                for p in selected
            },
            "rejections": rejected,
            "rejection_counts": dict(Counter(r for rs in rejected.values() for r in rs)),
            "outranked": [],
            "selection_scope": "all_qualified",
        }
        payload = await asyncio.to_thread(create_session, day, payload, expand=existing is not None)
        STATUS.update(payload)
        _restore_session_board(payload)
        return payload


async def evaluate_symbol(symbol: str, ctx: ScanContext, *, publish: bool = True) -> PipelineResult:
    from agents.market.agent import assess_market
    from risk.context_builder import build_risk_context
    from risk.limits import default_risk_limits
    from risk.risk_engine import RiskEngine
    from trading.final_admission import build_and_evaluate_final_admission
    from trading.final_pretrade import PretradeRejection
    from trading.ledger import LEDGER
    from trading.market_gate import evaluate_market_gate
    from trading.sector_assessment import get_sector_assessment_port

    now = datetime.now(UTC)
    symbol = symbol.upper()
    result = PipelineResult(pipeline_run_id=uuid4(), symbol=symbol, status="wait_for_entry")
    stored = await asyncio.to_thread(read_session, str(now.astimezone(ET).date()))
    if stored is None or symbol not in stored.get("plans", {}):
        return result.model_copy(update={"status": "no_trade", "errors": ["ORB_NOT_SELECTED"]})
    plan = OrbPlan.model_validate(stored["plans"][symbol])
    prior = stored.get("states", {}).get(symbol, {})
    if prior.get("opportunity_id"):
        from strategy.orb.reentry import rearm_closed_trade

        if await rearm_closed_trade(plan.session, symbol, ctx.broker, now=now):
            refreshed = await asyncio.to_thread(read_session, plan.session)
            if refreshed is None or symbol not in refreshed.get("plans", {}):
                return result.model_copy(
                    update={"status": "data_blocked", "errors": ["ORB_SESSION_UNRESOLVED"]}
                )
            stored = refreshed
            plan = OrbPlan.model_validate(stored["plans"][symbol])
            prior = stored["states"][symbol]
    if plan.version == VERSION:
        from uuid import UUID

        from strategy.orb.retest import rebuild
        from strategy.orb.retest_data import read_bars
        from strategy.orb.store import replace_unclaimed_plan
        from trading.opportunities import OPPORTUNITIES

        linked = (
            await asyncio.to_thread(OPPORTUNITIES.get, UUID(prior["opportunity_id"]))
            if prior.get("opportunity_id")
            else None
        )
        if linked and (
            linked.submitted_at is not None
            or linked.auto_trigger_last_outcome == "UNKNOWN"
            or linked.status
            not in {
                OpportunityStatus.AWAITING_CONFIRMATION,
                OpportunityStatus.SKIPPED,
                OpportunityStatus.DISCARDED,
                OpportunityStatus.EXPIRED,
            }
        ):
            return result.model_copy(
                update={
                    "status": linked.status.value,
                    "opportunity": linked,
                    "candidate": linked.candidate,
                }
            )
        after = datetime.fromisoformat(prior["retest_after"]) if prior.get("retest_after") else None
        reset = linked is not None and linked.status in {
            OpportunityStatus.SKIPPED,
            OpportunityStatus.DISCARDED,
            OpportunityStatus.EXPIRED,
        }
        if linked is not None and reset and prior.get("retest_reset_id") != str(linked.id):
            after = now + timedelta(seconds=60)
            await asyncio.to_thread(
                update_state,
                plan.session,
                symbol,
                {"retest_after": after.isoformat(), "retest_reset_id": str(linked.id)},
            )
        rows = await read_bars(ctx.market_data, plan, now=now, cached=True)
        now = datetime.now(UTC)
        revised = rebuild(plan, rows, now=now, after=after)
        revised_state: dict[str, Any] = {
            "state": revised.state,
            "reasons": revised.reasons,
            "observed_at": now.isoformat(),
        }
        if rows:
            revised_state.update(
                _bar_observation(max(rows, key=lambda bar: bar.ts), observed_at=now)
            )
        if revised.plan is None:
            await asyncio.to_thread(update_state, plan.session, symbol, revised_state)
            return result.model_copy(update={"status": "no_trade", "errors": revised.reasons})
        if after:
            revised_state["retest_after"] = after.isoformat()
        if revised.plan and (revised.plan != plan or reset):
            if not await asyncio.to_thread(
                replace_unclaimed_plan,
                plan.session,
                symbol,
                plan.model_dump(mode="json"),
                revised.plan.model_dump(mode="json"),
                revised_state,
                now=now,
            ):
                return result
            plan = revised.plan
            prior = (
                ((await asyncio.to_thread(read_session, plan.session)) or {})
                .get("states", {})
                .get(symbol, {})
            )
        if plan.evidence.get("retest"):
            quoter = getattr(ctx.market_data, "get_quote", None)
            current = await quoter(symbol) if quoter else None
            checked_at = datetime.now(UTC)
            checked = evaluate_trigger(plan, current, now=checked_at)
            target = Decimal(plan.evidence["retest"]["target"])
            if (
                current is not None
                and checked.state != "DATA_BLOCKED"
                and (current.bid <= plan.stop or current.bid >= target)
            ):
                reset_plan = rebuild(plan, rows, now=checked_at, after=checked_at).plan
                if reset_plan is not None:
                    replaced = await asyncio.to_thread(
                        replace_unclaimed_plan,
                        plan.session,
                        symbol,
                        plan.model_dump(mode="json"),
                        reset_plan.model_dump(mode="json"),
                        {
                            "state": "WAIT",
                            "reasons": ["ORB_RETEST_INVALIDATED"],
                            "retest_after": checked_at.isoformat(),
                        },
                        now=checked_at,
                    )
                    if replaced:
                        return result
                return result.model_copy(
                    update={"status": "data_blocked", "errors": ["ORB_RETEST_INVALIDATED"]}
                )
        if revised.state == "DATA_BLOCKED" or not plan.evidence.get("retest"):
            quoter = getattr(ctx.market_data, "get_quote", None)
            snapshots = getattr(ctx, "observation_snapshots", None)
            watched_quote = await quoter(symbol) if quoter and snapshots is None else None
            if snapshots is not None:
                snap = snapshots.get(symbol)
                revised_state.update(bid=None, ask=None, quote_at=None)
                if snap and snap.bid is not None and snap.ask is not None and snap.quote_ts:
                    revised_state.update(
                        bid=str(snap.bid), ask=str(snap.ask), quote_at=snap.quote_ts.isoformat()
                    )
            if watched_quote:
                revised_state.update(
                    bid=str(watched_quote.bid),
                    ask=str(watched_quote.ask),
                    quote_at=watched_quote.ts.isoformat(),
                )
            await asyncio.to_thread(update_state, plan.session, symbol, revised_state)
            return result.model_copy(
                update={
                    "status": "data_blocked"
                    if revised.state == "DATA_BLOCKED"
                    else "wait_for_entry",
                    "errors": revised.reasons,
                }
            )
    if prior.get("opportunity_id"):
        # Executed/unknown claims stay consumed; a skipped plan requires a fresh reset.
        from uuid import UUID

        from trading.opportunities import OPPORTUNITIES

        opp = await asyncio.to_thread(OPPORTUNITIES.get, UUID(prior["opportunity_id"]))
        if opp is None:
            await asyncio.to_thread(
                update_state,
                plan.session,
                symbol,
                {"state": "DATA_BLOCKED", "reasons": ["ORB_PUBLICATION_UNRESOLVED"]},
            )
            return result.model_copy(
                update={"status": "data_blocked", "errors": ["ORB_PUBLICATION_UNRESOLVED"]}
            )
        if opp.status is OpportunityStatus.SKIPPED:
            from strategy.orb.store import rearm_skipped_plan, upgrade_unpublished_entry_limits

            quoter = getattr(ctx.market_data, "get_quote", None)
            quote = await quoter(symbol) if quoter else None
            if await asyncio.to_thread(
                rearm_skipped_plan, plan.session, symbol, str(opp.id), quote, now=now
            ):
                from core.config import get_settings
                from core.enums import BrokerEnvironment

                if get_settings().broker_env is BrokerEnvironment.PAPER:
                    await asyncio.to_thread(upgrade_unpublished_entry_limits, plan.session, now=now)
            return result
        if opp.status is not OpportunityStatus.AWAITING_CONFIRMATION:
            await asyncio.to_thread(
                update_state,
                plan.session,
                symbol,
                {"state": opp.status.value.upper(), "reasons": ["ORB_ATTEMPT_CONSUMED"]},
            )
        elif now >= plan.entry_deadline:
            await asyncio.to_thread(
                update_state,
                plan.session,
                symbol,
                {"state": "NO_TRADE", "reasons": ["ORB_ENTRY_EXPIRED"]},
            )
        if opp is not None:
            return result.model_copy(
                update={"status": opp.status.value, "opportunity": opp, "candidate": opp.candidate}
            )
    if await asyncio.to_thread(LEDGER.find_open_by_symbol, symbol) is not None:
        return result.model_copy(update={"status": "position_open"})
    quoter = getattr(ctx.market_data, "get_quote", None)
    quote = await quoter(symbol) if quoter else None
    now = datetime.now(UTC)
    trigger = evaluate_trigger(plan, quote, now=now)
    state: dict[str, Any] = {
        "state": trigger.state,
        "reasons": trigger.reasons,
        "observed_at": now.isoformat(),
        "bid": str(quote.bid) if quote else None,
        "ask": str(quote.ask) if quote else None,
        "quote_at": quote.ts.isoformat() if quote else None,
    }
    await asyncio.to_thread(update_state, plan.session, symbol, state)
    if trigger.state != "BUY_ALLOWED" or quote is None:
        return result.model_copy(
            update={
                "status": {
                    "WAIT": "wait_for_entry",
                    "NO_TRADE": "no_trade",
                    "DATA_BLOCKED": "data_blocked",
                }[trigger.state],
                "errors": trigger.reasons,
            }
        )
    held = await ctx.broker.list_positions()
    if any(p.symbol.upper() == symbol and p.qty != 0 for p in held):
        await asyncio.to_thread(
            update_state,
            plan.session,
            symbol,
            {**state, "state": "NO_TRADE", "reasons": ["POSITION_ALREADY_OPEN"]},
        )
        return result.model_copy(
            update={"status": "position_open", "errors": ["POSITION_ALREADY_OPEN"]}
        )
    candidate = TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        confidence=0,
        entry=plan.max_entry,
        stop=plan.stop,
        exit_policy="session_close",
        exit_at=plan.exit_at,
        orb_plan=plan.model_dump(mode="json"),
        reasons=trigger.reasons,
        strategy_version=plan.version,
        exec_timeframe=Timeframe.M5,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        entry_decision=EntryDecision.BUY_NOW,
        admission_version=plan.version,
        policy_version=plan.version,
        pipeline_run_id=result.pipeline_run_id,
    )
    BOARD.set_agent("context", status="working", detail="Checking market regime", symbol=symbol)
    market = await assess_market(ctx.settings.fred_api_key, now=now)
    gate = evaluate_market_gate(market, now=now, require_sector=False)
    sector = await get_sector_assessment_port().assess(symbol, market_data=ctx.market_data, now=now)
    BOARD.set_agent(
        "context",
        status="done",
        detail="Market context ready" if gate.tradable_long else "Market context blocked",
        symbol=symbol,
        score=market.score,
    )
    BOARD.set_agent("checklist", status="working", detail="Running final admission", symbol=symbol)
    try:
        final = await build_and_evaluate_final_admission(
            candidate,
            quote=quote,
            market_data=ctx.market_data,
            now=now,
            market=market,
            sector_label=sector.sector_regime.value if sector.sector_regime else None,
            sector_tradable=sector.tradable_long,
            sector_benchmark=sector.benchmark,
            sector_provider=sector.market_data_provider,
            sector_source_ts=sector.benchmark_last_bar_ts,
        )
    except PretradeRejection as exc:
        BOARD.set_agent("checklist", status="error", detail=str(exc), symbol=symbol)
        await asyncio.to_thread(
            update_state,
            plan.session,
            symbol,
            {**state, "state": "BLOCKED", "reasons": [str(exc)]},
        )
        return result.model_copy(
            update={"candidate": candidate, "status": "data_blocked", "errors": [str(exc)]}
        )
    BOARD.set_agent("checklist", status="done", detail="Final admission passed", symbol=symbol)
    BOARD.set_agent("risk", status="working", detail="Evaluating portfolio risk", symbol=symbol)
    built = await build_risk_context(
        symbol,
        broker=ctx.broker,
        market_data=ctx.market_data,
        finnhub_api_key=ctx.settings.finnhub_api_key,
        regime_tradable=gate.tradable_long,
        now=now,
    )
    risk = RiskEngine(default_risk_limits()).evaluate(
        candidate, await ctx.portfolio(), context=built.context
    )
    result = result.model_copy(
        update={
            "candidate": candidate,
            "market": market,
            "risk": risk,
            "trade_admission": final.admission,
        }
    )
    if risk.verdict != RiskVerdict.PASS:
        BOARD.set_agent(
            "risk",
            status="done",
            detail="Risk rejected · " + ", ".join(risk.reasons[:2]),
            symbol=symbol,
            score=0,
        )
        await asyncio.to_thread(
            update_state,
            plan.session,
            symbol,
            {**state, "state": "BLOCKED", "reasons": risk.reasons},
        )
        return result.model_copy(update={"status": "risk_rejected", "errors": risk.reasons})
    BOARD.set_agent("risk", status="done", detail="Risk passed", symbol=symbol, score=100)
    if not publish:
        return result.model_copy(update={"status": "risk_passed"})
    from strategy.orb.publication import publish_orb

    opp = await asyncio.to_thread(publish_orb, result, final, ctx.settings.trading_mode)
    from core.audit import create_audit
    from trading.auto_trigger_policy import enqueue_auto_approve_opportunity

    enqueue_auto_approve_opportunity(opp.id, audit=create_audit(), symbol=symbol)
    return result.model_copy(update={"status": "awaiting_confirmation", "opportunity": opp})


async def observe(context: ScanContext | None = None) -> dict[str, int]:
    """Join the one observation pass in flight instead of returning fake emptiness.

    Both the scanner cadence and the five-second watch loop need the latest
    result. Duplicate callers await one task and receive its real counts.
    """
    global _observation_task

    task = _observation_task
    if task is None or task.done():
        task = asyncio.create_task(_observe_once(context), name="orb-observation")
        _observation_task = task
    try:
        return await asyncio.shield(task)
    finally:
        if _observation_task is task and task.done():
            _observation_task = None


async def observe_priority(context: ScanContext | None = None) -> dict[str, int]:
    """Evaluate completed bars without waiting for the full-universe reconciler.

    Most symbols are still at phase 1. Their new bar can be rejected from the
    immutable opening-range geometry in memory and persisted in one database
    transaction. Only symbols that can advance (plus already-ready entries)
    enter the heavier per-symbol admission path.
    """
    global _last_ready_check

    if _priority_lock.locked():
        return {}
    async with _priority_lock:
        with _pending_completed_bars_lock:
            pending = dict(_pending_completed_bars)
            _pending_completed_bars.clear()
        now = datetime.now(UTC)
        stored = await asyncio.to_thread(read_session, str(now.astimezone(ET).date()))
        if stored is None:
            return {}
        plans = stored.get("plans") or {}
        states = stored.get("states") or {}
        by_symbol: dict[str, list[Bar]] = {}
        for (symbol, _ts), bar in pending.items():
            if symbol in plans:
                by_symbol.setdefault(symbol, []).append(bar)

        ready_due = asyncio.get_running_loop().time() - _last_ready_check >= 5
        ready_symbols = {
            symbol
            for symbol, raw in plans.items()
            if (raw.get("evidence") or {}).get("retest")
            and not states.get(symbol, {}).get("opportunity_id")
            and states.get(symbol, {}).get("state")
            not in {"NO_TRADE", "EXECUTED", "APPROVED", "DISCARDED", "EXPIRED"}
        }
        if not by_symbol and not (ready_due and ready_symbols):
            return {}
        if ready_due:
            _last_ready_check = asyncio.get_running_loop().time()

        # The stream can deliver a current bar after a restart while older bars
        # are still absent.  A non-breakout current bar is only a valid fast-path
        # decision when the durable series is contiguous from 09:35 ET.  Without
        # this guard an earlier breakout is silently lost and the UI lies with a
        # fresh-looking WAIT_BREAKOUT state.
        from strategy.orb.retest_data import history_complete

        history_checks = asyncio.Semaphore(8)

        async def complete(symbol: str) -> tuple[str, bool]:
            async with history_checks:
                plan = OrbPlan.model_validate(plans[symbol])
                value = await asyncio.to_thread(history_complete, plan, now=now)
                return symbol, value

        history_ready = dict(await asyncio.gather(*(complete(symbol) for symbol in by_symbol)))
        candidates = set(ready_symbols if ready_due else ())
        passive: dict[str, dict[str, Any]] = {}
        for symbol, bars in by_symbol.items():
            plan = OrbPlan.model_validate(plans[symbol])
            state = states.get(symbol, {})
            latest = max(bars, key=lambda bar: bar.ts)
            wait_breakout = state.get("reasons") == ["ORB_RETEST_WAIT_BREAKOUT"]
            can_breakout = any(
                bar.ts >= plan.range_end
                and bar.close > plan.range_high + Decimal("0.01")
                and bar.close > bar.open
                for bar in bars
            )
            if (
                wait_breakout
                and not can_breakout
                and history_ready.get(symbol, False)
                and now < plan.entry_deadline
                and not state.get("opportunity_id")
            ):
                passive[symbol] = {
                    "state": "WAIT",
                    "reasons": ["ORB_RETEST_WAIT_BREAKOUT"],
                    **_bar_observation(latest, observed_at=now),
                }
            else:
                candidates.add(symbol)

        if passive:
            await asyncio.to_thread(update_states, stored["session"], passive)

        counts: Counter[str] = Counter(wait_for_entry=len(passive))
        if candidates:
            from contextlib import AsyncExitStack

            async with AsyncExitStack() as stack:
                ctx = (
                    context
                    if context is not None
                    else await stack.enter_async_context(open_scan_context(get_settings()))
                )
                from strategy.orb.retest_data import prime_bars

                candidate_plans = [OrbPlan.model_validate(plans[s]) for s in sorted(candidates)]
                await prime_bars(ctx.market_data, candidate_plans, now=now)
                snapshots = getattr(ctx.market_data, "get_snapshots", None)
                if callable(snapshots):
                    try:
                        ctx.observation_snapshots = await asyncio.wait_for(
                            snapshots(sorted(candidates)), timeout=10
                        )
                    except Exception:  # noqa: BLE001 — quotes cannot authorize entries
                        ctx.observation_snapshots = {}
                for symbol in sorted(candidates):
                    try:
                        result = await evaluate_symbol(symbol, ctx)
                        counts[result.status] += 1
                    except Exception as exc:  # noqa: BLE001 — one symbol fails closed
                        reason = data_error_reason(exc, feed=stored.get("feed", "sip"))
                        await asyncio.to_thread(
                            update_state,
                            stored["session"],
                            symbol,
                            {
                                "state": "DATA_BLOCKED",
                                "reasons": [reason],
                                "observed_at": datetime.now(UTC).isoformat(),
                                "bid": None,
                                "ask": None,
                                "quote_at": None,
                            },
                        )
                        counts["data_blocked"] += 1

        from core.desk_bus import DESK_BUS

        refreshed = await asyncio.to_thread(read_session, stored["session"])
        if refreshed is not None:
            STATUS.update(refreshed)
        DESK_BUS.bump_desk(kind="orb_priority_observation")
        return dict(counts)


async def _observe_once(context: ScanContext | None = None) -> dict[str, int]:
    """Perform one complete persisted ORB observation pass."""
    from contextlib import AsyncExitStack

    from core.desk_bus import DESK_BUS

    now = datetime.now(UTC)
    stored = await asyncio.to_thread(read_session, str(now.astimezone(ET).date()))
    if stored is None:
        return {}
    plans = stored.get("plans") or {}
    _restore_session_board(stored)
    BOARD.set_agent("setup", status="working", detail=f"Monitoring {len(plans)} ORB setups")
    BOARD.set_agent("entry", status="working", detail=f"Checking {len(plans)} entry triggers")
    counts: Counter[str] = Counter()

    async with AsyncExitStack() as stack:
        ctx = (
            context
            if context is not None
            else await stack.enter_async_context(open_scan_context(get_settings()))
        )
        from strategy.orb.retest_data import prime_bars

        try:
            await prime_bars(
                ctx.market_data,
                [OrbPlan.model_validate(p) for p in plans.values()],
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — a failed batch blocks observation
            reason = data_error_reason(exc, feed=stored.get("feed", "sip"))
            BOARD.set_agent("setup", status="error", detail=reason)
            BOARD.set_agent("entry", status="error", detail=reason)
            BOARD.log("scanner", f"ORB history unavailable: {reason}", level="warn")
            for symbol in plans:
                await asyncio.to_thread(
                    update_state,
                    stored["session"],
                    symbol,
                    {
                        "state": "DATA_BLOCKED",
                        "reasons": [reason],
                        "observed_at": datetime.now(UTC).isoformat(),
                        "bid": None,
                        "ask": None,
                        "quote_at": None,
                    },
                )
            refreshed = await asyncio.to_thread(read_session, stored["session"])
            if refreshed is not None:
                STATUS.update(refreshed)
            DESK_BUS.bump_desk(kind="orb_observation")
            return {"data_blocked": len(plans)}
        snapshots = getattr(ctx.market_data, "get_snapshots", None)
        if callable(snapshots):
            try:
                ctx.observation_snapshots = await asyncio.wait_for(
                    snapshots(list(plans)), timeout=10
                )
            except Exception:  # noqa: BLE001 — display quotes do not authorize entries
                # Observation quotes are optional display data. Every actual
                # entry still requires its own fresh quote/admission.
                ctx.observation_snapshots = {}
        last_symbol: str | None = None
        for symbol in plans:
            last_symbol = symbol
            try:
                result = await evaluate_symbol(symbol, ctx)
                counts[result.status] += 1
            except Exception as exc:  # noqa: BLE001 — a failed input blocks this symbol
                reason = data_error_reason(exc, feed=stored.get("feed", "sip"))
                previous = stored.get("states", {}).get(symbol, {})
                if previous.get("state") != "DATA_BLOCKED" or previous.get("reasons") != [reason]:
                    BOARD.log(
                        "scanner",
                        f"ORB observation unavailable: {reason}",
                        symbol=symbol,
                        level="warn",
                    )
                await asyncio.to_thread(
                    update_state,
                    stored["session"],
                    symbol,
                    {
                        "state": "DATA_BLOCKED",
                        "reasons": [reason],
                        "bid": None,
                        "ask": None,
                        "quote_at": None,
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                )
                counts["data_blocked"] += 1

    summary = " · ".join(f"{key} {value}" for key, value in sorted(counts.items())) or "no plans"
    BOARD.set_agent("setup", status="done", detail=f"ORB setups checked · {summary}")
    BOARD.set_agent(
        "entry",
        status="done",
        detail=f"Entry triggers checked · {summary}",
        symbol=last_symbol,
    )
    refreshed = await asyncio.to_thread(read_session, stored["session"])
    if refreshed is not None:
        STATUS.update(refreshed)
    DESK_BUS.bump_desk(kind="orb_observation")
    return dict(counts)


def retire_pending_legacy() -> int:
    """Withdraw only unclaimed legacy proposals; never mutate fills or in-flight intents."""
    from trading.opportunities import OPPORTUNITIES

    count = 0
    for opp in OPPORTUNITIES.list_open():
        if (
            opp.candidate.strategy_version not in SUPPORTED_VERSIONS
            and opp.status is OpportunityStatus.AWAITING_CONFIRMATION
        ):
            OPPORTUNITIES.update(opp.model_copy(update={"status": OpportunityStatus.DISCARDED}))
            count += 1
    return count
