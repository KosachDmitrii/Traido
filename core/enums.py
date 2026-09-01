"""Shared enums — capital-path critical values are explicit and narrow."""

from enum import StrEnum


class BrokerEnvironment(StrEnum):
    PAPER = "paper"
    LIVE = "live"  # not wired in V1


class TradingMode(StrEnum):
    CONFIRMATION = "confirmation"
    AUTOPILOT = "autopilot"


class TradeAction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    """Normalized *broker* order status. Adapters map native strings onto this."""

    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class IntentStatus(StrEnum):
    """Traido's own view of an order intent — durable, and wider than the broker's.

    The broker can only tell us what it knows. `UNKNOWN` is the state for when
    it cannot, and it is deliberately not terminal: it means "truth unresolved",
    which blocks conflicting trading until reconciliation settles it.

    Spelling note: the spec calls the cancelled state CANCELLED. This codebase
    already spells it `OrderStatus.CANCELED`, so both use one spelling rather
    than shipping two words that differ by a single letter.
    """

    CREATED = "created"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class IntentPurpose(StrEnum):
    """Why an order exists. Never inferred from BUY/SELL.

    A SELL is three different things depending on intent: a discretionary exit,
    a resting protective stop, and a forced flatten after protection failed.
    They have different idempotency keys, different blocking rules, and
    different urgency, so the reason is recorded rather than guessed.
    """

    ENTRY = "entry"
    EXIT = "exit"
    EMERGENCY_EXIT = "emergency_exit"
    PROTECTIVE_EXIT = "protective_exit"


EXIT_PURPOSES: frozenset[IntentPurpose] = frozenset(
    {IntentPurpose.EXIT, IntentPurpose.EMERGENCY_EXIT}
)
"""Purposes that reduce exposure by an unknown amount until the broker answers.

A protective stop is deliberately excluded: it rests at the broker by design,
so its being unresolved is the normal state and must not block anything.
"""


class BrokerConnectionState(StrEnum):
    """How much we can currently trust the broker link.

    Only READY permits opening or discretionarily changing exposure. The others
    still permit reconciliation, because the way out of an ambiguous state is
    to read more, not to trade more.
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"


class EarningsCheck(StrEnum):
    """Whether the earnings calendar was actually consulted for a symbol.

    Without this, `next_earnings=None` says two irreconcilable things at once:
    "the calendar was read and there is no print scheduled" and "nobody looked".
    The first is a cleared check; the second is an unchecked risk that looks
    identical to a cleared one, which is how a trade gets held through a print.

    Only CHECKED is a check. The other three are the reasons there isn't one,
    kept apart because they need different responses: NOT_CONFIGURED is fixed by
    an operator adding a key, UNAVAILABLE is a vendor outage that may clear on
    its own, and NOT_CHECKED means the caller never asked at all.
    """

    CHECKED = "checked"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    NOT_CHECKED = "not_checked"


class NewsCheck(StrEnum):
    """Whether the headlines were actually read for a symbol.

    The strategy vetoes on negative sentiment, so news is a gate. That makes a
    neutral score ambiguous in the same way `next_earnings=None` was: it means
    both "we read the headlines and nothing was wrong" and "we never got to
    look". Only the first is a check.

    Split by what the operator would do about it, for the same reason as
    `EarningsCheck`: NOT_CONFIGURED is a missing key and clears every symbol
    once fixed, UNAVAILABLE is a vendor outage that resolves itself, and
    NOT_CHECKED means the caller never asked.
    """

    CHECKED = "checked"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    NOT_CHECKED = "not_checked"


class SectorCheck(StrEnum):
    """Whether the candidate's sector was actually established.

    The sector cap is a real limit, and a limit is only enforced on a name we
    can place in a bucket. `core.universe.sector_of` answers `"unknown"` for a
    name outside the curated file, and that string used to travel into the risk
    engine as though it were a sector — where it did two wrong things at once.
    It collided genuinely unrelated names into one bucket, and, worse, it let a
    name *out* of its real sector's cap: an unmapped technology name landed in
    the empty `"unknown"` bucket and passed a technology limit that was already
    full. `"unknown"` is not a sector, it is the absence of one.

    Split by what the operator would do about it, as `NewsCheck` is:
    UNCLASSIFIED means the curated file and Finnhub both left the name without
    a mapped industry; NOT_CONFIGURED is a missing Finnhub key for a name the
    file does not cover; UNAVAILABLE is a vendor outage; NOT_CHECKED means the
    caller never asked.
    """

    CHECKED = "checked"
    UNCLASSIFIED = "unclassified"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    NOT_CHECKED = "not_checked"


class OpportunityStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVING = "approving"  # claimed for execution (idempotent lock)
    APPROVED = "approved"
    SKIPPED = "skipped"
    EXPIRED = "expired"
    EXECUTED = "executed"
    DISCARDED = "discarded"


class InstrumentThesis(StrEnum):
    """Directional view on the instrument. Never implies an executable entry."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class EntryDecision(StrEnum):
    """Whether the current price is an acceptable entry given a thesis.

    BULLISH != BUY_NOW. WAIT_FOR_ENTRY keeps a valid thesis without offering a
    brokerable card. NO_TRADE ends the setup.
    """

    BUY_NOW = "buy_now"
    WAIT_FOR_ENTRY = "wait_for_entry"
    NO_TRADE = "no_trade"


class EntryWatchStatus(StrEnum):
    WAITING = "waiting"
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    CONVERTED = "converted"  # became a BUY_NOW opportunity after fresh re-check
    CANCELLED = "cancelled"


class TargetReachabilityClass(StrEnum):
    REALISTIC = "realistic"
    AMBITIOUS = "ambitious"
    UNREALISTIC = "unrealistic"
    INSUFFICIENT_DATA = "insufficient_data"


class SessionCohort(StrEnum):
    """When a signal was born. RTH entry stats must not mix with premarket."""

    PREMARKET = "premarket"
    RTH = "rth"
    AFTER_HOURS = "after_hours"
    UNKNOWN = "unknown"


class RiskVerdict(StrEnum):
    PASS = "pass"
    REJECT = "reject"


class AssessmentKind(StrEnum):
    TECHNICAL = "technical"
    NEWS = "news"
    MARKET = "market"
    QUANT = "quant"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Timeframe(StrEnum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class MarketRegimeLabel(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    HIGH_VOLATILITY = "high_volatility"
    RISK_OFF = "risk_off"
    RISK_ON = "risk_on"


class UserDecision(StrEnum):
    APPROVE = "approve"
    SKIP = "skip"
    HOLD = "hold"
    SELL = "sell"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    SIGNAL = "signal"
    END_OF_DATA = "end_of_data"


class BacktestRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UniverseMode(StrEnum):
    """How wide the live universe is. Mirrors `universe.models.UniverseTier`.

    Duplicated as a settings enum so `core.config` does not import the universe
    package — configuration is read at startup by everything, and a cycle
    through the instrument layer would make that ordering fragile.
    """

    CORE = "CORE"
    EXTENDED = "EXTENDED"
    BROAD = "BROAD"
