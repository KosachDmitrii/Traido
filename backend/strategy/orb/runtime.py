"""One persisted opening-range selection per session, observed independently of risk."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, cast
from uuid import uuid4

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
from strategy.orb.store import create_session, read_session, update_state
from trading.scan_context import ScanContext, open_scan_context
from trading.session_hours import us_equity_rth_open
from universe.models import UniverseTier
from universe.service import UniverseService

_discovery_lock = asyncio.Lock()
_observation_lock = asyncio.Lock()
STATUS: dict[str, Any] = {"status": "not_started", "version": VERSION, "parameters": PARAMETERS}


async def discover(
    ctx: ScanContext, universe: UniverseService, *, now: datetime | None = None
) -> dict[str, Any]:
    """Keep all qualifying plans, ordered by opening relative volume."""
    now = now or datetime.now(UTC)
    day = str(now.astimezone(ET).date())
    async with _discovery_lock:
        feed_name = getattr(ctx.market_data, "_feed", None)
        existing = read_session(day)
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
                existing = upgrade_unpublished_entry_limits(day, now=now) or existing
                STATUS.update(existing)
            if existing.get("selection_scope") == "all_qualified":
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
        if feed_name not in {"iex", "sip"} or not callable(getattr(feed, "get_bars_batch", None)):
            STATUS.update(status="data_blocked", reason="ORB_UNSUPPORTED_FEED")
            return dict(STATUS)
        STATUS.update(status="loading_history", reason=None)
        snapshot = await universe.get_scan_universe(tier=UniverseTier.BROAD, max_size=0)
        symbols = snapshot.symbols
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
            mean_dollars = sum((b.close * b.volume for b in bs[-14:]), Decimal(0)) / 14
            volume_low = (
                adv < 1000000
                if feed_name == "sip"
                else mean_dollars < Decimal(str(PARAMETERS["iex_min_avg_dollar_volume"]))
            )
            if volume_low or atr <= Decimal("0.50"):
                counts["base_rejected"] += 1
                rejected[symbol] = (
                    ["ORB_DAILY_VOLUME_LOW" if feed_name == "sip" else "ORB_IEX_DOLLAR_VOLUME_LOW"]
                    if volume_low
                    else []
                ) + (["ORB_ATR_LOW"] if atr <= Decimal("0.50") else [])
            else:
                base.append(symbol)
        opening: dict[str, list[Bar]] = {s: [] for s in base}
        # Only 15 five-minute windows, not 45 days of intraday data for the universe.
        STATUS["status"] = "loading_opening_ranges"
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
                p.symbol: {"state": "WAIT", "reasons": ["ORB_WAITING_BREAKOUT"]} for p in selected
            },
            "rejections": rejected,
            "rejection_counts": dict(Counter(r for rs in rejected.values() for r in rs)),
            "outranked": [],
            "selection_scope": "all_qualified",
        }
        payload = create_session(day, payload, expand=existing is not None)
        STATUS.update(payload)
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
    stored = read_session(str(now.astimezone(ET).date()))
    if stored is None or symbol not in stored.get("plans", {}):
        return result.model_copy(update={"status": "no_trade", "errors": ["ORB_NOT_SELECTED"]})
    plan = OrbPlan.model_validate(stored["plans"][symbol])
    prior = stored.get("states", {}).get(symbol, {})
    if prior.get("opportunity_id"):
        # Executed/unknown claims stay consumed; a skipped plan requires a fresh reset.
        from uuid import UUID

        from trading.opportunities import OPPORTUNITIES

        opp = OPPORTUNITIES.get(UUID(prior["opportunity_id"]))
        if opp is None:
            update_state(
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
            if rearm_skipped_plan(plan.session, symbol, str(opp.id), quote, now=now):
                from core.config import get_settings
                from core.enums import BrokerEnvironment

                if get_settings().broker_env is BrokerEnvironment.PAPER:
                    upgrade_unpublished_entry_limits(plan.session, now=now)
            return result
        if opp.status is not OpportunityStatus.AWAITING_CONFIRMATION:
            update_state(
                plan.session,
                symbol,
                {"state": opp.status.value.upper(), "reasons": ["ORB_ATTEMPT_CONSUMED"]},
            )
        elif now >= plan.entry_deadline:
            update_state(
                plan.session, symbol, {"state": "NO_TRADE", "reasons": ["ORB_ENTRY_EXPIRED"]}
            )
        if opp is not None:
            return result.model_copy(
                update={"status": opp.status.value, "opportunity": opp, "candidate": opp.candidate}
            )
    if LEDGER.find_open_by_symbol(symbol) is not None:
        return result.model_copy(update={"status": "position_open"})
    quoter = getattr(ctx.market_data, "get_quote", None)
    quote = await quoter(symbol) if quoter else None
    trigger = evaluate_trigger(plan, quote, now=now)
    state: dict[str, Any] = {
        "state": trigger.state,
        "reasons": trigger.reasons,
        "observed_at": now.isoformat(),
        "bid": str(quote.bid) if quote else None,
        "ask": str(quote.ask) if quote else None,
        "quote_at": quote.ts.isoformat() if quote else None,
    }
    update_state(plan.session, symbol, state)
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
        update_state(
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
        reasons=["ORB_BREAKOUT_CONFIRMED"],
        strategy_version=plan.version,
        exec_timeframe=Timeframe.M5,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        entry_decision=EntryDecision.BUY_NOW,
        admission_version=plan.version,
        policy_version=plan.version,
        pipeline_run_id=result.pipeline_run_id,
    )
    market = await assess_market(ctx.settings.fred_api_key, now=now)
    gate = evaluate_market_gate(market, now=now, require_sector=False)
    sector = await get_sector_assessment_port().assess(symbol, market_data=ctx.market_data, now=now)
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
        update_state(plan.session, symbol, {**state, "state": "BLOCKED", "reasons": [str(exc)]})
        return result.model_copy(
            update={"candidate": candidate, "status": "data_blocked", "errors": [str(exc)]}
        )
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
        update_state(plan.session, symbol, {**state, "state": "BLOCKED", "reasons": risk.reasons})
        return result.model_copy(update={"status": "risk_rejected", "errors": risk.reasons})
    if not publish:
        return result.model_copy(update={"status": "risk_passed"})
    from strategy.orb.publication import publish_orb

    opp = publish_orb(result, final, ctx.settings.trading_mode)
    from core.audit import create_audit
    from trading.auto_trigger_policy import enqueue_auto_approve_opportunity

    enqueue_auto_approve_opportunity(opp.id, audit=create_audit(), symbol=symbol)
    return result.model_copy(update={"status": "awaiting_confirmation", "opportunity": opp})


async def observe(context: ScanContext | None = None) -> dict[str, int]:
    """No direct order placement. Each observed trigger goes to manual confirmation."""
    from core.activity import BOARD
    from core.desk_bus import DESK_BUS

    if _observation_lock.locked():
        return {}
    async with _observation_lock:
        now = datetime.now(UTC)
        stored = read_session(str(now.astimezone(ET).date()))
        if stored is None:
            return {}
        counts: Counter[str] = Counter()
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            ctx = (
                context
                if context is not None
                else await stack.enter_async_context(open_scan_context(get_settings()))
            )
            for symbol in stored.get("plans", {}):
                try:
                    result = await evaluate_symbol(symbol, ctx)
                    counts[result.status] += 1
                except Exception as exc:  # noqa: BLE001 — a failed input blocks this symbol
                    BOARD.log(
                        "scanner",
                        f"ORB observation unavailable: {type(exc).__name__}",
                        symbol=symbol,
                        level="warn",
                    )
                    update_state(
                        stored["session"],
                        symbol,
                        {
                            "state": "DATA_BLOCKED",
                            "reasons": ["ORB_SERVICE_UNAVAILABLE"],
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    counts["data_blocked"] += 1
        STATUS.update(read_session(stored["session"]) or {})
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
