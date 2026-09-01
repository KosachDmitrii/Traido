"""Stage 1 — the cheap market filter that runs over the whole universe.

Everything here is decided from one batched snapshot read. No news, no
fundamentals, no LLM, no portfolio, no per-symbol request. That constraint is
the point: this stage sees a thousand names, so anything it touches is touched a
thousand times, and the measured cost of touching a name the expensive way was
3.95 seconds and 12.6 HTTP requests.

Rejecting here is always safe. Stage 1 can only *remove* candidates, and every
gate that protects capital — liquidity, RTH, risk, earnings, news — still runs
in full on whatever survives. So the failure mode to design against is not
letting something bad through, it is throwing away something good for a reason
that is really a data problem. Hence: mandatory facts are mandatory, and their
absence is a rejection with a name, never a zero that reads as "illiquid".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from core.schemas import Snapshot
from universe.models import Instrument


class MarketFilterReason(StrEnum):
    """Why a name did not survive Stage 1. Counted, so codes not sentences."""

    NO_SNAPSHOT = "NO_SNAPSHOT"
    """The batch came back without this symbol at all."""

    MISSING_PRICE = "MISSING_PRICE"
    MISSING_VOLUME = "MISSING_VOLUME"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_BARS = "INVALID_BARS"
    CROSSED_BOOK = "CROSSED_BOOK"
    STALE_DATA = "STALE_DATA"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    PRICE_ABOVE_MAXIMUM = "PRICE_ABOVE_MAXIMUM"
    INSUFFICIENT_DOLLAR_VOLUME = "INSUFFICIENT_DOLLAR_VOLUME"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    OUTRANKED_BY_LIMIT = "OUTRANKED_BY_LIMIT"
    """Survived every test and still lost a place to the prefilter cap.

    A separate reason from every other one here, because it says nothing about
    the instrument. A desk that sees this rising is a desk whose cap is too low,
    which is a different problem from a universe full of illiquid names.
    """


@dataclass(frozen=True)
class MarketFilterPolicy:
    """Stage 1 thresholds. Every one configurable; none of them business truth."""

    min_price: Decimal = Decimal(5)
    max_price: Decimal | None = Decimal(10000)
    min_dollar_volume: Decimal = Decimal(20_000_000)
    """Today's traded value floor.

    Deliberately measured on the session in progress rather than on an average:
    a name that normally trades $50M and has done $2M by mid-afternoon is not
    liquid today, and today is when the order would go in. Average dollar volume
    is measured in Stage 2, where the daily history has already been fetched.
    """

    max_spread_bps: float = 50.0
    """Screening-wide, not the execution threshold.

    The liquidity gate's 30 bps is checked against a *live* quote at the click.
    This one reads a snapshot that may be a few seconds old and only exists to
    drop names whose book is hopeless, so it is looser on purpose. Making it
    equal would reject candidates on stale data that the real gate would pass.
    """

    max_data_age_sec: float = 900.0
    """How old the snapshot's last trade may be.

    Fifteen minutes: long enough that a quiet name mid-session is not thrown
    out, short enough that a symbol which stopped printing hours ago is. A feed
    that stopped returns a pass, not an error, which is the failure this catches.
    """

    require_quote: bool = False
    """Whether a missing book is fatal at this stage.

    Off by default, and that is not a weakening of the liquidity gate: that gate
    fails closed on a missing live quote at execution and is untouched. Stage 1
    reads a batched snapshot whose quote side is thinner than the live one — 748
    of 844 names had a usable book in a live run — and rejecting the other 96
    here would discard candidates the real gate would have priced correctly.
    """


@dataclass
class MarketFilterCandidate:
    """One symbol's Stage 1 verdict with the numbers behind it.

    `measured` is kept for every candidate, passed or rejected, because the
    funnel has to be able to answer "why was nothing found today" with values
    and not just with counts.
    """

    symbol: str
    instrument: Instrument
    snapshot: Snapshot | None = None
    passed: bool = False
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    measured: dict[str, float] = field(default_factory=dict)


def _age_sec(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


def evaluate_snapshot(
    instrument: Instrument,
    snapshot: Snapshot | None,
    policy: MarketFilterPolicy,
    *,
    now: datetime,
) -> MarketFilterCandidate:
    """Judge one name. Pure, deterministic, no I/O."""
    symbol = instrument.key
    if snapshot is None:
        return MarketFilterCandidate(
            symbol=symbol,
            instrument=instrument,
            reasons=(MarketFilterReason.NO_SNAPSHOT,),
        )

    reasons: list[str] = []
    measured: dict[str, float] = {}

    price = snapshot.price
    volume = snapshot.day_volume

    if price is None:
        reasons.append(MarketFilterReason.MISSING_PRICE)
    elif price <= 0:
        reasons.append(MarketFilterReason.INVALID_PRICE)
    else:
        measured["price"] = float(price)

    if volume is None:
        reasons.append(MarketFilterReason.MISSING_VOLUME)
    elif volume < 0:
        reasons.append(MarketFilterReason.INVALID_BARS)
    else:
        measured["day_volume"] = float(volume)

    # A high below its low is not a thin session, it is a broken record, and a
    # broken record is exactly what a thousand-name universe produces daily.
    if (
        snapshot.day_high is not None
        and snapshot.day_low is not None
        and snapshot.day_high < snapshot.day_low
    ):
        reasons.append(MarketFilterReason.INVALID_BARS)

    if snapshot.bid is not None and snapshot.ask is not None and snapshot.ask < snapshot.bid:
        reasons.append(MarketFilterReason.CROSSED_BOOK)

    age = _age_sec(snapshot.trade_ts, now)
    if age is not None:
        measured["data_age_sec"] = age
        if age > policy.max_data_age_sec or age < -policy.max_data_age_sec:
            reasons.append(MarketFilterReason.STALE_DATA)

    if price is not None and price > 0:
        if price < policy.min_price:
            reasons.append(MarketFilterReason.PRICE_BELOW_MINIMUM)
        elif policy.max_price is not None and price > policy.max_price:
            reasons.append(MarketFilterReason.PRICE_ABOVE_MAXIMUM)

    dollar_volume = 0.0
    if price is not None and price > 0 and volume is not None and volume >= 0:
        dollar_volume = float(price * volume)
        measured["dollar_volume"] = dollar_volume
        if Decimal(str(dollar_volume)) < policy.min_dollar_volume:
            reasons.append(MarketFilterReason.INSUFFICIENT_DOLLAR_VOLUME)

    spread = snapshot.spread_bps
    if spread is not None:
        measured["spread_bps"] = spread
        if spread > policy.max_spread_bps:
            reasons.append(MarketFilterReason.SPREAD_TOO_WIDE)
    elif policy.require_quote:
        reasons.append(MarketFilterReason.CROSSED_BOOK)

    return MarketFilterCandidate(
        symbol=symbol,
        instrument=instrument,
        snapshot=snapshot,
        passed=not reasons,
        score=dollar_volume,
        reasons=tuple(reasons),
        measured=measured,
    )


@dataclass
class MarketFilterOutcome:
    passed: list[MarketFilterCandidate] = field(default_factory=list)
    rejected: list[MarketFilterCandidate] = field(default_factory=list)

    @property
    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in self.rejected:
            for reason in candidate.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return counts


def apply_market_filter(
    instruments: Sequence[Instrument],
    snapshots: dict[str, Snapshot],
    *,
    policy: MarketFilterPolicy | None = None,
    limit: int = 0,
    now: datetime | None = None,
) -> MarketFilterOutcome:
    """Stage 1 over the whole universe, then cut to `limit` deterministically.

    The cut is by traded value and then by symbol, never by arrival order. A
    prefilter that kept whichever names a provider happened to answer for first
    would make the desk's output depend on network timing, which is the same
    defect as ranking by completion order and is harder to notice here.
    """
    pol = policy or MarketFilterPolicy()
    when = now or datetime.now(UTC)
    outcome = MarketFilterOutcome()

    for instrument in instruments:
        candidate = evaluate_snapshot(instrument, snapshots.get(instrument.key), pol, now=when)
        if candidate.passed:
            outcome.passed.append(candidate)
        else:
            outcome.rejected.append(candidate)

    outcome.passed.sort(key=lambda c: (-c.score, c.symbol))
    if limit > 0 and len(outcome.passed) > limit:
        for cut in outcome.passed[limit:]:
            cut.passed = False
            cut.reasons = (*cut.reasons, MarketFilterReason.OUTRANKED_BY_LIMIT)
            outcome.rejected.append(cut)
        outcome.passed = outcome.passed[:limit]
    return outcome


def symbols_of(candidates: Iterable[MarketFilterCandidate]) -> list[str]:
    return [c.symbol for c in candidates]
