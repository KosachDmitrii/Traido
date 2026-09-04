"""
Pre-execution gates.

Everything upstream of execution is looking for a reason to trade. These two
gates look for a reason not to send the order, and they run last, immediately
before the broker call, so nothing can route around them.

Both are deterministic: same inputs, same verdict, no network, no model. Their
thresholds live in code and config rather than in a prompt, because a gate an
LLM can talk its way past is not a gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from core.enums import BrokerConnectionState
from core.schemas import Bar, Quote
from market_data.entry_spread import spread_bps_for_entry
from quant.filters import TradabilityLimits, check_tradability
from quant.volatility import average_dollar_volume
from trading.session_hours import SessionPhase, session_phase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateResult:
    """Structured verdict: what was decided, why, and on what numbers."""

    gate: str
    passed: bool
    reasons: tuple[str, ...] = ()
    measured: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "measured": self.measured,
            "thresholds": self.thresholds,
        }


# ── Spread measurement ───────────────────────────────────────────────────────


class SpreadSource(StrEnum):
    """Where a spread number came from. Never elided.

    The distinction is the whole point: a modeled spread is a backtest
    assumption, and reporting one as though the market had confirmed it turns a
    guess into a passed safety check.
    """

    LIVE = "live"
    MODELED = "modeled"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SpreadReading:
    source: SpreadSource
    bps: float | None = None
    age_sec: float | None = None

    @property
    def is_live(self) -> bool:
        return self.source is SpreadSource.LIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "spread_source": self.source.value,
            "spread_bps": self.bps,
            "quote_age_sec": self.age_sec,
        }


SPREAD_UNAVAILABLE = SpreadReading(source=SpreadSource.UNAVAILABLE)


def measure_spread(
    quote: Quote | None,
    *,
    now: datetime,
    max_age_sec: float,
    last_price: float | None = None,
    feed: str | None = None,
) -> SpreadReading:
    """Turn a top-of-book snapshot into a spread, or say why it could not.

    A quote older than `max_age_sec` is reported STALE rather than used: in a
    fast tape a two-minute-old spread describes a market that no longer exists.

    When ``last_price`` is supplied on IEX, spread uses buy-side friction vs
    the last print when it sits inside the book (see ``entry_spread``).
    """
    if quote is None:
        return SPREAD_UNAVAILABLE

    age = (now - quote.ts).total_seconds()
    bps = spread_bps_for_entry(quote, last_price=last_price, feed=feed)
    if bps is None:
        return SPREAD_UNAVAILABLE

    if age > max_age_sec or age < -max_age_sec:
        return SpreadReading(source=SpreadSource.STALE, bps=bps, age_sec=age)
    return SpreadReading(source=SpreadSource.LIVE, bps=bps, age_sec=age)


def modeled_spread(bps: float) -> SpreadReading:
    """For backtests and research only. Marked so it can never pass a live gate."""
    return SpreadReading(source=SpreadSource.MODELED, bps=bps)


# ── Liquidity ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiquidityPolicy:
    """V1 policy. Deliberately strict — we would rather skip than be trapped."""

    min_price: Decimal = Decimal(10)
    """Penny stocks are blocked outright; the floor is well above any of them."""
    max_price: Decimal = Decimal(10_000)
    min_avg_dollar_volume: float = 20_000_000.0
    min_bar_dollar_volume: float = 1_000_000.0
    """Today's tape, not just the 20-day average — liquidity can evaporate."""
    max_spread_bps: float = 30.0
    max_participation_pct: float = 1.0
    """Our order as a share of average daily dollar volume."""
    max_estimated_slippage_bps: float = 25.0
    min_bars: int = 20
    allowed_symbols: frozenset[str] | None = None
    """When set, membership is what blocks OTC: those names are never listed."""

    require_live_spread: bool = True
    """Fail closed when the spread cannot be verified against a live quote.

    This is the honest default for a live entry. Research and backtest callers
    turn it off deliberately and get a reading marked MODELED, which is a
    different claim from "the market confirmed this spread".
    """
    max_quote_age_sec: float = 15.0


def check_liquidity(
    symbol: str,
    bars: list[Bar],
    *,
    qty: Decimal,
    price: Decimal,
    spread: SpreadReading | None = None,
    policy: LiquidityPolicy | None = None,
) -> GateResult:
    """Decide whether this specific order can be executed and exited sanely."""
    pol = policy or LiquidityPolicy()
    ticker = symbol.upper()
    reasons: list[str] = []
    measured: dict[str, Any] = {"symbol": ticker, "price": str(price), "qty": str(qty)}

    if pol.allowed_symbols is not None and ticker not in pol.allowed_symbols:
        reasons.append("SYMBOL_NOT_IN_UNIVERSE")

    if price < pol.min_price:
        reasons.append("PRICE_BELOW_FLOOR")
    if price > pol.max_price:
        reasons.append("PRICE_ABOVE_CEILING")

    if len(bars) < pol.min_bars:
        reasons.append("INSUFFICIENT_HISTORY")
        measured["bars"] = len(bars)
        return GateResult(
            gate="liquidity",
            passed=False,
            reasons=tuple(reasons),
            measured=measured,
            thresholds=_liquidity_thresholds(pol),
        )

    adv = average_dollar_volume(bars, 20)
    measured["avg_dollar_volume"] = adv
    if adv is None or adv < pol.min_avg_dollar_volume:
        reasons.append("INSUFFICIENT_AVG_DOLLAR_VOLUME")

    last = bars[-1]
    bar_dollars = float(last.close) * float(last.volume)
    measured["last_bar_dollar_volume"] = bar_dollars
    if bar_dollars < pol.min_bar_dollar_volume:
        reasons.append("INSUFFICIENT_CURRENT_VOLUME")

    reading = spread or SPREAD_UNAVAILABLE
    measured.update(reading.as_dict())
    if reading.bps is not None and reading.bps > pol.max_spread_bps:
        reasons.append("SPREAD_TOO_WIDE")
    if pol.require_live_spread and not reading.is_live:
        # Not "the spread looked fine" — we could not see the spread at all.
        # For a live entry that is a rejection, not a silent pass.
        reasons.append(
            "QUOTE_STALE" if reading.source is SpreadSource.STALE else "LIVE_QUOTE_REQUIRED"
        )

    notional = float(qty) * float(price)
    measured["notional"] = notional
    if adv and adv > 0:
        participation = notional / adv * 100.0
        measured["participation_pct"] = participation
        if participation > pol.max_participation_pct:
            reasons.append("ORDER_TOO_LARGE_FOR_LIQUIDITY")
        # Square-root impact: the standard first-order estimate, and enough to
        # reject an order that would move the price it is trying to get.
        slippage_bps = 10.0 * (participation / 100.0) ** 0.5 * 100.0
        measured["estimated_slippage_bps"] = slippage_bps
        if slippage_bps > pol.max_estimated_slippage_bps:
            reasons.append("ESTIMATED_SLIPPAGE_TOO_HIGH")

    return GateResult(
        gate="liquidity",
        passed=not reasons,
        reasons=tuple(reasons),
        measured=measured,
        thresholds=_liquidity_thresholds(pol),
    )


def _liquidity_thresholds(pol: LiquidityPolicy) -> dict[str, Any]:
    return {
        "min_price": str(pol.min_price),
        "max_price": str(pol.max_price),
        "min_avg_dollar_volume": pol.min_avg_dollar_volume,
        "min_bar_dollar_volume": pol.min_bar_dollar_volume,
        "max_spread_bps": pol.max_spread_bps,
        "max_participation_pct": pol.max_participation_pct,
        "max_estimated_slippage_bps": pol.max_estimated_slippage_bps,
        "require_live_spread": pol.require_live_spread,
        "max_quote_age_sec": pol.max_quote_age_sec,
    }


def check_tradability_gate(
    symbol: str,
    bars: list[Bar],
    *,
    target: Decimal | None = None,
    stop: Decimal | None = None,
    limits: TradabilityLimits | None = None,
) -> GateResult:
    """Reuse the existing scanner-side tradability checks at execution time."""
    result = check_tradability(symbol, bars, limits=limits, target=target, stop=stop)
    return GateResult(
        gate="tradability",
        passed=result.passed,
        reasons=tuple(result.rejections),
        measured={
            "avg_dollar_volume": result.avg_dollar_volume,
            "atr_pct": result.atr_pct,
            "edge_to_cost_ratio": result.edge_to_cost_ratio,
            "notes": result.notes,
        },
    )


# ── Data freshness ───────────────────────────────────────────────────────────

MAX_BAR_AGE_SEC = 5 * 24 * 3600.0
"""How old the newest bar may be before the series is refused.

Five days rather than one: bars stop arriving over a weekend and every market
holiday, and a threshold that fired on a Tuesday after a long weekend would be
switched off within the week. Five covers the longest ordinary gap — Friday's
close to Tuesday's open is under four days — and still catches a feed that has
genuinely stopped.

One number serves the daily and the intraday series because the gap that sets
it is the same for both: the market being closed. It is deliberately loose for
an hourly series, and that is a stated limitation rather than an oversight —
an hourly feed three days behind still passes. It exists to catch the failure
that actually happened, a vendor serving the *oldest* page of a requested
window, which put the hourly series seven weeks out.
"""


def check_bar_freshness(
    symbol: str,
    bars: list[Bar],
    *,
    now: datetime,
    max_age_sec: float = MAX_BAR_AGE_SEC,
) -> GateResult:
    """Refuse an entry measured against a series that has stopped updating.

    The quote behind the spread check has a fifteen-second age limit. The daily
    bars behind average dollar volume, the price floor and expected slippage had
    none — so a feed that silently stopped a week ago produced a liquidity
    verdict that read as a pass, computed from a week-old market.

    `check_tradability` has held a `STALE_DATA` check the whole time and no
    caller; this is the gate that asks it at the point capital moves.
    """
    if not bars:
        return GateResult(
            gate="data_freshness",
            passed=False,
            reasons=("NO_BARS",),
            measured={"symbol": symbol},
            thresholds={"max_age_sec": max_age_sec},
        )

    newest = max(b.ts for b in bars)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    age = (now - newest).total_seconds()
    return GateResult(
        gate="data_freshness",
        passed=age <= max_age_sec,
        reasons=() if age <= max_age_sec else ("STALE_BARS",),
        measured={"symbol": symbol, "age_sec": round(age), "newest_bar": newest.isoformat()},
        thresholds={"max_age_sec": max_age_sec},
    )


# ── Instrument eligibility ───────────────────────────────────────────────────

_OTC_SUFFIXES = ("F", "Y")
"""Trailing letters on a five-character ticker that mark an OTC listing.

A crude test, and knowingly so: this is a backstop for the Alpaca path, where
the adapter reports no security type and `allowed_symbols` defaults to `None`,
so nothing else stands between a scanner and an unlisted name. The IBKR path
resolves a real contract and should assert against `secType` instead.
"""


def check_instrument_eligibility(
    symbol: str,
    *,
    allowed_symbols: frozenset[str] | None = None,
    security_type: str | None = None,
    currency: str | None = None,
) -> GateResult:
    """Refuse anything that is not a plain US-listed equity in USD.

    V1 sizes positions, sets stops and models slippage on assumptions that hold
    for listed equities and nowhere else. An OTC name clears the liquidity gate
    on printed volume while having no book to exit into; an option or a future
    would be sized as though a share were a share.

    Reasons are returned together rather than at the first failure — an operator
    reading a refusal wants to know everything wrong with the symbol.
    """
    ticker = (symbol or "").strip().upper()
    reasons: list[str] = []

    if not ticker or not ticker.replace(".", "").isalpha():
        reasons.append("SYMBOL_NOT_A_PLAIN_EQUITY_TICKER")
    if allowed_symbols is not None and ticker not in allowed_symbols:
        reasons.append("SYMBOL_NOT_ALLOWED")
    if security_type is not None and security_type.upper() not in {"STK", "EQUITY", "US_EQUITY"}:
        reasons.append("SECURITY_TYPE_NOT_EQUITY")
    if currency is not None and currency.upper() != "USD":
        reasons.append("CURRENCY_NOT_USD")
    if security_type is None and len(ticker) == 5 and ticker.endswith(_OTC_SUFFIXES):
        reasons.append("SYMBOL_LOOKS_OTC")

    return GateResult(
        gate="instrument",
        passed=not reasons,
        reasons=tuple(reasons),
        measured={"symbol": ticker, "security_type": security_type, "currency": currency},
    )


# ── Regular trading hours ────────────────────────────────────────────────────

_PHASE_REASON = {
    SessionPhase.CLOSED_WEEKEND: "MARKET_CLOSED_WEEKEND",
    SessionPhase.CLOSED_HOLIDAY: "MARKET_CLOSED_HOLIDAY",
    SessionPhase.PREMARKET: "PREMARKET",
    SessionPhase.AFTER_HOURS: "AFTER_HOURS",
}


# ── Broker connectivity ──────────────────────────────────────────────────────


def check_connectivity(state: BrokerConnectionState) -> GateResult:
    """Gate actions that *increase or discretionarily change* exposure.

    Deliberately not applied to reconciliation or to protective orders already
    resting at the broker: when the link is shaky, the correct response is to
    read more and trade less, not to stop looking.
    """
    return GateResult(
        gate="connectivity",
        passed=state is BrokerConnectionState.READY,
        reasons=() if state is BrokerConnectionState.READY else (f"BROKER_{state.value.upper()}",),
        measured={"connection_state": state.value},
        thresholds={"required": BrokerConnectionState.READY.value},
    )


# ── Broker truth freshness ───────────────────────────────────────────────────


def check_reconciliation(age_sec: float | None, *, max_age_sec: float) -> GateResult:
    """Refuse new exposure taken against broker truth nobody has verified lately.

    Every other gate here reasons about the market. This one reasons about *us*:
    position count, open exposure, whether this symbol already has a position,
    whether an intent for it is unresolved — the risk engine judges all of it
    against a local book, and the only thing that keeps that book honest is
    reconciliation. Once it stops running, the numbers do not become obviously
    wrong; they become confidently wrong, which is worse.

    `age_sec is None` means no pass has ever succeeded — the state every process
    is in for its first few seconds. That is refused too, and named separately,
    because "never checked" and "checked an hour ago" call for different
    operator responses even though both block the trade.

    Deliberately scoped to *new* exposure. Protective exits, emergency closes
    and reconciliation itself are never gated on this: when broker truth is
    doubtful the correct response is to read more and take on less, not to stop
    defending what is already open.
    """
    passed = age_sec is not None and age_sec <= max_age_sec
    if age_sec is None:
        reasons: tuple[str, ...] = ("RECONCILIATION_NEVER_RAN",)
    elif not passed:
        reasons = ("RECONCILIATION_STALE",)
    else:
        reasons = ()
    return GateResult(
        gate="reconciliation",
        passed=passed,
        reasons=reasons,
        measured={"age_sec": age_sec},
        thresholds={"max_age_sec": max_age_sec},
    )


def check_rth(now: datetime | None = None) -> GateResult:
    """Gate *new entries* only.

    Protective exits and reconciliation deliberately do not consult this gate:
    refusing to interact with the broker outside RTH would strand an open
    position, which is the opposite of safe.
    """
    ts = now or datetime.now(UTC)
    phase = session_phase(ts)
    reason = _PHASE_REASON.get(phase)
    return GateResult(
        gate="rth",
        passed=reason is None,
        reasons=() if reason is None else (reason,),
        measured={"phase": phase.value, "at": ts.isoformat()},
        thresholds={"session": "09:30-16:00 America/New_York, early closes honored"},
    )
