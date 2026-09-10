"""Deterministic, long-only opening-range breakout policy."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.clock import ET
from core.schemas import Bar, Quote
from trading.session_hours import is_market_holiday, session_close, us_equity_rth_open

VERSION = "orb@1.3.0"
SUPPORTED_VERSIONS = frozenset({"orb@1.1.0", "orb@1.2.0", VERSION})
# Paper implementation parameters; statistical profitability is not certified.
PARAMETERS = {
    "opening_minutes": 5,
    "lookback_sessions": 14,
    "min_price": "5",
    "sip_min_daily_volume": 1000000,
    "iex_min_avg_dollar_volume": "20000000",
    "min_daily_atr": "0.50",
    "min_relative_volume": "1",
    "selection_scope": "all_qualified",
    "stop_atr_fraction": "0.10",
    "direction": "long_only",
    "exit": "session_close",
    "exit_buffer_seconds": 60,
    "entry_cutoff_minutes_before_exit": 5,
    "max_entry_drift_r": "1.0",
    "entry_policy_revision": "paper-flex-1",
    "supported_feeds": ["iex", "sip"],
    "default_paper_feed": "iex",
}


FLEX_PARAMETERS = {k: v for k, v in PARAMETERS.items() if k != "selection_scope"}
FLEX_PARAMETERS["top_n"] = 20
LEGACY_PARAMETERS = {k: v for k, v in FLEX_PARAMETERS.items() if k != "entry_policy_revision"}
LEGACY_PARAMETERS["max_entry_drift_r"] = "0.25"


class OrbPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str
    session: str
    version: str = VERSION
    range_start: datetime
    range_end: datetime
    range_open: Decimal
    range_high: Decimal
    range_low: Decimal
    range_close: Decimal
    opening_volume: Decimal
    mean_opening_volume: Decimal
    relative_volume: Decimal
    daily_atr: Decimal
    mean_daily_volume: Decimal
    trigger: Decimal
    stop: Decimal
    max_entry: Decimal
    entry_deadline: datetime
    exit_at: datetime
    source: str = "alpaca:sip"
    # Explicit immutable inputs, including prior completed sessions.
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_plan(self) -> OrbPlan:
        numbers = (
            self.range_open,
            self.range_high,
            self.range_low,
            self.range_close,
            self.opening_volume,
            self.mean_opening_volume,
            self.relative_volume,
            self.daily_atr,
            self.mean_daily_volume,
            self.trigger,
            self.stop,
            self.max_entry,
        )
        if any(not n.is_finite() or n <= 0 for n in numbers):
            raise ValueError("ORB_INVALID_GEOMETRY")
        if not (self.stop < self.trigger <= self.max_entry) or self.trigger <= self.range_high:
            raise ValueError("ORB_INVALID_GEOMETRY")
        if any(
            t.tzinfo is None
            for t in (self.range_start, self.range_end, self.entry_deadline, self.exit_at)
        ):
            raise ValueError("ORB_TIMEZONE_REQUIRED")
        if not self.range_start < self.range_end < self.entry_deadline < self.exit_at:
            raise ValueError("ORB_INVALID_TIMES")
        if self.session != str(self.range_start.astimezone(ET).date()):
            raise ValueError("ORB_SESSION_MISMATCH")
        return self


class OrbDecision(BaseModel):
    state: Literal["WAIT", "BUY_ALLOWED", "NO_TRADE", "DATA_BLOCKED"]
    reasons: list[str]
    plan: OrbPlan | None = None
    measured: dict[str, Any] = Field(default_factory=dict)


def _valid_bar(b: Bar) -> bool:
    values = (b.open, b.high, b.low, b.close, b.volume)
    return (
        b.ts.tzinfo is not None
        and all(Decimal(str(v)).is_finite() for v in values)
        and b.low > 0
        and b.low <= min(b.open, b.close)
        and b.high >= max(b.open, b.close)
        and b.volume >= 0
    )


def _previous_sessions(now: datetime, count: int) -> list[date]:
    day = now.astimezone(ET).date()
    result: list[date] = []
    while len(result) < count:
        day -= timedelta(days=1)
        if day.weekday() < 5 and not is_market_holiday(day):
            result.append(day)
    return list(reversed(result))


def form_plan(
    symbol: str,
    daily: list[Bar],
    opening_bars: list[Bar],
    *,
    now: datetime,
    feed: str,
    version: str = VERSION,
) -> OrbDecision:
    """Use only complete opening bars and prior complete daily sessions."""

    def blocked(reason: str) -> OrbDecision:
        return OrbDecision(state="DATA_BLOCKED", reasons=[reason])

    if version not in SUPPORTED_VERSIONS:
        return blocked("ORB_INVALID_PROVENANCE")
    parameters = (
        PARAMETERS
        if version == VERSION
        else (FLEX_PARAMETERS if version == "orb@1.2.0" else LEGACY_PARAMETERS)
    )
    if now.tzinfo is None:
        return blocked("ORB_TIMEZONE_REQUIRED")
    if feed not in {"iex", "sip"}:
        return blocked("ORB_UNSUPPORTED_FEED")
    local = now.astimezone(ET)
    start = datetime.combine(local.date(), time(9, 30), ET)
    end = start + timedelta(minutes=5)
    exit_at = datetime.combine(local.date(), session_close(local.date()), ET) - timedelta(
        seconds=60
    )
    deadline = exit_at - timedelta(minutes=5)
    if not us_equity_rth_open(now) or now >= deadline:
        return OrbDecision(state="NO_TRADE", reasons=["ORB_OUTSIDE_ENTRY_SESSION"])
    if now < end:
        return OrbDecision(state="WAIT", reasons=["ORB_OPENING_RANGE_FORMING"])

    # Reject contradictory duplicates instead of choosing a convenient revision.
    def by_day(bars: list[Bar], opening: bool) -> dict[date, Bar]:
        result: dict[date, Bar] = {}
        for b in bars:
            if b.symbol.upper() != symbol.upper() or not _valid_bar(b):
                raise ValueError("ORB_INVALID_BAR")
            ts = b.ts.astimezone(ET)
            if opening and (ts.time() != time(9, 30) or b.timeframe.value != "5m"):
                continue
            if not opening and b.timeframe.value != "1d":
                raise ValueError("ORB_DAILY_TIMEFRAME_INVALID")
            if ts.date() in result and result[ts.date()] != b:
                raise ValueError("ORB_CONFLICTING_BARS")
            if b.ts + (timedelta(minutes=5) if opening else timedelta()) <= now:
                result[ts.date()] = b
        return result

    try:
        d, o = by_day(daily, False), by_day(opening_bars, True)
    except ValueError as exc:
        return blocked(str(exc))
    days = _previous_sessions(now, 15)
    if any(day not in d for day in days) or any(day not in o for day in days[-14:]):
        return blocked("ORB_HISTORY_INCOMPLETE")
    if local.date() not in o:
        return blocked("ORB_OPENING_RANGE_MISSING")
    today = o[local.date()]
    prior = [d[day] for day in days]
    ranges = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in pairwise(prior)
    ]
    atr = sum(ranges, Decimal(0)) / Decimal(14)
    mean_volume = sum((b.volume for b in prior[-14:]), Decimal(0)) / Decimal(14)
    mean_open = sum((o[day].volume for day in days[-14:]), Decimal(0)) / Decimal(14)
    if mean_open <= 0 or atr <= 0:
        return blocked("ORB_INVALID_BASELINE")
    rv = today.volume / mean_open
    reasons = []
    if today.open <= 5:
        reasons.append("ORB_PRICE_BELOW_MINIMUM")
    if feed == "sip" and mean_volume < 1000000:
        reasons.append("ORB_DAILY_VOLUME_LOW")
    mean_dollars = sum((b.close * b.volume for b in prior[-14:]), Decimal(0)) / 14
    if feed == "iex" and mean_dollars < Decimal(str(parameters["iex_min_avg_dollar_volume"])):
        reasons.append("ORB_IEX_DOLLAR_VOLUME_LOW")
    if atr <= Decimal("0.50"):
        reasons.append("ORB_ATR_LOW")
    if rv < 1:
        reasons.append("ORB_RELATIVE_VOLUME_LOW")
    if today.close <= today.open:
        reasons.append("ORB_OPENING_NOT_BULLISH")
    measured = {
        "relative_volume": str(rv),
        "daily_atr": str(atr),
        "mean_daily_volume": str(mean_volume),
        "mean_daily_dollar_volume": str(mean_dollars),
        "feed": feed,
    }
    if reasons:
        return OrbDecision(state="NO_TRADE", reasons=reasons, measured=measured)
    trigger = (today.high + Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    stop = (trigger - atr * Decimal("0.10")).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    if stop <= 0 or stop >= trigger:
        return blocked("ORB_INVALID_STOP")
    max_entry = (
        trigger + (trigger - stop) * Decimal(str(parameters["max_entry_drift_r"]))
    ).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    plan = OrbPlan(
        symbol=symbol.upper(),
        version=version,
        session=str(local.date()),
        range_start=start,
        range_end=end,
        range_open=today.open,
        range_high=today.high,
        range_low=today.low,
        range_close=today.close,
        opening_volume=today.volume,
        mean_opening_volume=mean_open,
        relative_volume=rv,
        daily_atr=atr,
        mean_daily_volume=mean_volume,
        source=f"alpaca:{feed}",
        trigger=trigger,
        stop=stop,
        max_entry=max_entry,
        entry_deadline=deadline,
        exit_at=exit_at,
        evidence={
            "daily": [b.model_dump(mode="json") for b in prior],
            "opening": [o[day].model_dump(mode="json") for day in days[-14:]]
            + [today.model_dump(mode="json")],
            "atr_method": "mean_of_14_true_ranges",
            "parameters": dict(parameters),
        },
    )
    return OrbDecision(state="WAIT", reasons=["ORB_WAITING_BREAKOUT"], plan=plan, measured=measured)


def evaluate_trigger(
    plan: OrbPlan, quote: Quote | None, *, now: datetime, limit_price: Decimal | None = None
) -> OrbDecision:
    def result(
        state: Literal["WAIT", "BUY_ALLOWED", "NO_TRADE", "DATA_BLOCKED"], reason: str
    ) -> OrbDecision:
        return OrbDecision(state=state, reasons=[reason], plan=plan)

    if (
        now.tzinfo is None
        or plan.version not in SUPPORTED_VERSIONS
        or plan.source not in {"alpaca:iex", "alpaca:sip"}
    ):
        return result("DATA_BLOCKED", "ORB_INVALID_PROVENANCE")
    if (
        not us_equity_rth_open(now)
        or now >= plan.entry_deadline
        or str(now.astimezone(ET).date()) != plan.session
    ):
        return result("NO_TRADE", "ORB_ENTRY_EXPIRED")
    if now < plan.range_end:
        return result("WAIT", "ORB_OPENING_RANGE_FORMING")
    if quote is None or quote.symbol.upper() != plan.symbol or quote.ts.tzinfo is None:
        return result("DATA_BLOCKED", "ORB_QUOTE_MISSING")
    if quote.feed is not None and plan.source != f"alpaca:{quote.feed}":
        return result("DATA_BLOCKED", "ORB_DATA_FEED_MISMATCH")
    age = (now - quote.ts).total_seconds()
    if age < -1 or age > 5:
        return result("DATA_BLOCKED", "ORB_QUOTE_STALE")
    if (
        not quote.bid.is_finite()
        or not quote.ask.is_finite()
        or quote.bid <= 0
        or quote.ask < quote.bid
    ):
        return result("DATA_BLOCKED", "ORB_QUOTE_INVALID")
    # Bid above the trigger avoids treating a widening offer as a breakout.
    if quote.bid < plan.trigger:
        return result("WAIT", "ORB_WAITING_BREAKOUT")
    if quote.ask > plan.max_entry or (limit_price is not None and limit_price > plan.max_entry):
        return result("NO_TRADE", "ORB_ENTRY_MISSED")
    if limit_price is not None and limit_price < quote.ask:
        return result("DATA_BLOCKED", "ORB_LIMIT_BELOW_OFFER")
    return result("BUY_ALLOWED", "ORB_BREAKOUT_CONFIRMED")
