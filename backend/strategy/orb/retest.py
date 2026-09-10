"""Paper experiment: completed-bar breakout, retest and distinct confirmation.

Replay only information available at evaluation time. Entry geometry is frozen
at confirmation, with a short expiry. Existing source bars remain immutable.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from core.enums import Timeframe
from core.schemas import Bar, Quote
from strategy.orb import PARAMETERS, VERSION, OrbDecision, OrbPlan, _valid_bar

CENT = Decimal("0.01")


def rebuild(
    base: OrbPlan, bars: list[Bar], *, now: datetime, after: datetime | None = None
) -> OrbDecision:
    if (
        base.version != VERSION
        or now.tzinfo is None
        or (after is not None and after.tzinfo is None)
    ):
        return OrbDecision(state="DATA_BLOCKED", reasons=["ORB_INVALID_PROVENANCE"])
    # Discard old derived geometry before replay; an invalidated ready plan must
    # never remain executable merely because it was the input to this function.
    from strategy.orb import form_plan

    initial = form_plan(
        base.symbol,
        [Bar.model_validate(b) for b in base.evidence["daily"]],
        [Bar.model_validate(b) for b in base.evidence["opening"]],
        now=now,
        feed=base.source.removeprefix("alpaca:"),
        version=VERSION,
    )
    if initial.plan is None:
        return OrbDecision(state="NO_TRADE", reasons=["ORB_RETEST_INVALIDATED"])
    base = initial.plan

    def result(state, reason, plan=None):
        return OrbDecision(state=state, reasons=[reason], plan=plan or base)

    if any(
        b.ts.tzinfo is None
        or b.symbol != base.symbol
        or b.timeframe != Timeframe.M5
        or b.source != "alpaca"
        or not _valid_bar(b)
        for b in bars
    ):
        return result("DATA_BLOCKED", "ORB_RETEST_DATA_INVALID")
    rows = sorted(
        [b for b in bars if b.ts >= base.range_end and b.ts + timedelta(minutes=5) <= now],
        key=lambda b: b.ts,
    )
    expected = base.range_end
    for b in rows:
        if b.ts != expected:
            return result("DATA_BLOCKED", "ORB_RETEST_HISTORY_GAP")
        expected += timedelta(minutes=5)
    if now >= expected + timedelta(minutes=5):
        return result("DATA_BLOCKED", "ORB_RETEST_BARS_STALE")
    band = max(CENT, base.daily_atr * Decimal(PARAMETERS["retest_band_atr"]))
    buffer = max(CENT, base.daily_atr * Decimal(PARAMETERS["retest_stop_buffer_atr"]))
    level = base.range_high
    breakout = None
    retest = None
    peak = None
    low = None
    ready = None
    wait_reason = "ORB_RETEST_WAIT_BREAKOUT"
    for b in rows:
        end = b.ts + timedelta(minutes=5)
        if after and end <= after:
            continue
        if ready is not None:
            if (
                end <= datetime.fromisoformat(ready.evidence["retest"]["valid_until"])
                and b.low > ready.stop
                and b.high < Decimal(ready.evidence["retest"]["target"])
            ):
                continue
            ready = None
            breakout = retest = None
            wait_reason = "ORB_RETEST_EXPIRED"
            # Do not use the same bar that invalidated a plan for another entry.
            continue
        if breakout is not None and end > breakout.ts + timedelta(
            minutes=5 + PARAMETERS["setup_timeout_minutes"]
        ):
            breakout = retest = None
            wait_reason = "ORB_RETEST_EXPIRED"
        if breakout is None:
            if b.close > level + CENT and b.close > b.open:
                breakout, peak = b, b.high
                wait_reason = "ORB_RETEST_WAIT_RETURN"
            continue
        if b.close < level - band:
            breakout = retest = None
            wait_reason = "ORB_RETEST_INVALIDATED"
            continue
        if retest is None:
            # Target uses the observed impulse BEFORE the retest, never future highs.
            if b.low <= level + band and b.high >= level - band:
                retest, low = b, b.low
                wait_reason = "ORB_RETEST_WAIT_CONFIRMATION"
            else:
                peak = max(peak, b.high)
            continue
        low = min(low, b.low)
        if b.close <= level + CENT or b.close <= b.open or b.close <= retest.close:
            continue
        stop = (low - buffer).quantize(CENT, rounding=ROUND_FLOOR)
        ceiling = min(b.close, level + band).quantize(CENT, rounding=ROUND_FLOOR)
        floor = (level + CENT).quantize(CENT, rounding=ROUND_CEILING)
        target = peak.quantize(CENT, rounding=ROUND_FLOOR)
        cost = ceiling * Decimal(PARAMETERS["cost_allowance_bps"]) / 10000
        risk = ceiling - stop
        reward = target - ceiling
        if (
            b.high >= target
            or not (0 < stop < floor <= ceiling < target)
            or (reward - cost) < Decimal(PARAMETERS["min_effective_reward_risk"]) * (risk + cost)
        ):
            breakout = retest = None
            wait_reason = "ORB_RETEST_REWARD_INSUFFICIENT"
            continue
        valid_until = min(
            base.entry_deadline, end + timedelta(minutes=PARAMETERS["signal_ttl_minutes"])
        )
        evidence = deepcopy(base.evidence)
        evidence["retest"] = {
            "phase": "ready",
            "confirmed_at": end.isoformat(),
            "valid_until": valid_until.isoformat(),
            "breakout_at": breakout.ts.isoformat(),
            "retest_at": retest.ts.isoformat(),
            "entry_min": str(floor),
            "target": str(target),
            "retest_low": str(low),
            "bars": [x.model_dump(mode="json") for x in rows if x.ts <= b.ts],
            "after": after.isoformat() if after else None,
            "time_exit_minutes": PARAMETERS["time_exit_minutes"],
            "cost_allowance_bps": PARAMETERS["cost_allowance_bps"],
        }
        raw = base.model_dump()
        raw.update(trigger=floor, max_entry=ceiling, stop=stop, evidence=evidence)
        ready = OrbPlan.model_validate(raw)
    if ready is not None and now < datetime.fromisoformat(ready.evidence["retest"]["valid_until"]):
        return result("WAIT", "ORB_RETEST_CONFIRMED", ready)
    return result("WAIT", "ORB_RETEST_EXPIRED" if ready else wait_reason)


def check_entry(
    plan: OrbPlan, quote: Quote, *, now: datetime, limit_price: Decimal | None
) -> OrbDecision:
    def result(state, reason):
        return OrbDecision(state=state, reasons=[reason], plan=plan)

    evidence = plan.evidence.get("retest", {})
    if evidence.get("phase") != "ready":
        return result("WAIT", "ORB_RETEST_WAIT_BREAKOUT")
    try:
        until = datetime.fromisoformat(evidence["valid_until"])
        confirmation = datetime.fromisoformat(evidence["confirmed_at"])
        target = Decimal(evidence["target"])
        if (
            until.tzinfo is None
            or confirmation.tzinfo is None
            or now < confirmation
            or not target.is_finite()
            or target <= plan.max_entry
            or until <= confirmation
            or until > plan.entry_deadline
            or until - confirmation > timedelta(minutes=PARAMETERS["signal_ttl_minutes"])
            or (limit_price is not None and (not limit_price.is_finite() or limit_price <= 0))
        ):
            raise ValueError("invalid confirmation")
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return result("DATA_BLOCKED", "ORB_RETEST_DATA_INVALID")
    if now >= until:
        return result("NO_TRADE", "ORB_RETEST_EXPIRED")
    if quote.bid <= plan.stop:
        return result("NO_TRADE", "ORB_STOP_BREACHED")
    if quote.bid < plan.trigger:
        return result("WAIT", "ORB_RETEST_WAIT_RECOVERY")
    if quote.ask > plan.max_entry or (limit_price is not None and limit_price > plan.max_entry):
        return result("WAIT", "ORB_WAITING_PULLBACK")
    if limit_price is not None and limit_price < quote.ask:
        return result("DATA_BLOCKED", "ORB_LIMIT_BELOW_OFFER")
    cost = max(
        quote.ask - quote.bid, plan.max_entry * Decimal(PARAMETERS["cost_allowance_bps"]) / 10000
    )
    if target - plan.max_entry - cost < Decimal(PARAMETERS["min_effective_reward_risk"]) * (
        plan.max_entry - plan.stop + cost
    ):
        return result("NO_TRADE", "ORB_RETEST_REWARD_INSUFFICIENT")
    return result("BUY_ALLOWED", "ORB_RETEST_CONFIRMED")
