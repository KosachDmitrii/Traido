"""
Order intent — Traido's durable record of "we mean to send this order".

The problem this solves is narrow and expensive: between deciding to buy and
knowing what the broker did, there is a window in which the process can die, the
connection can drop, or a retry can arrive. Without a record written *before*
transmission, recovery has to guess, and the two available guesses are "send it
again" (double position) and "assume nothing happened" (unprotected position).

So the intent is written first, keyed by `idempotency_key`, and every state
change is persisted. Recovery then reads the intent and asks the broker, rather
than guessing.

`UNKNOWN` is the state that makes this honest. It is not a failure and not a
success — it means broker truth is unresolved, and it blocks conflicting trading
for that symbol until reconciliation settles it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import Field

from core.enums import EXIT_PURPOSES, IntentPurpose, IntentStatus, OrderSide, OrderStatus, OrderType
from core.ports import BrokerPort
from core.schemas import OrderRecord, StrictModel

logger = logging.getLogger(__name__)

# ── State machine ────────────────────────────────────────────────────────────

ALLOWED_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.CREATED: frozenset({IntentStatus.SUBMITTING, IntentStatus.REJECTED}),
    # Submitting is the danger window: we may or may not have reached the broker.
    IntentStatus.SUBMITTING: frozenset(
        {
            IntentStatus.SUBMITTED,
            IntentStatus.REJECTED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.SUBMITTED: frozenset(
        {
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.CANCEL_PENDING,
            IntentStatus.CANCELED,
            IntentStatus.REJECTED,
            IntentStatus.EXPIRED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.ACKNOWLEDGED: frozenset(
        {
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.CANCEL_PENDING,
            IntentStatus.CANCELED,
            IntentStatus.REJECTED,
            IntentStatus.EXPIRED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.PARTIALLY_FILLED: frozenset(
        {
            IntentStatus.PARTIALLY_FILLED,  # more of the order filled
            IntentStatus.FILLED,
            IntentStatus.CANCEL_PENDING,
            IntentStatus.CANCELED,
            IntentStatus.EXPIRED,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.CANCEL_PENDING: frozenset(
        {
            IntentStatus.CANCELED,
            # A cancel can lose the race with a fill.
            IntentStatus.FILLED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.UNKNOWN,
        }
    ),
    # UNKNOWN is recoverable in every direction — that is the whole point.
    IntentStatus.UNKNOWN: frozenset(
        {
            IntentStatus.SUBMITTED,
            IntentStatus.ACKNOWLEDGED,
            IntentStatus.PARTIALLY_FILLED,
            IntentStatus.FILLED,
            IntentStatus.CANCELED,
            IntentStatus.REJECTED,
            IntentStatus.EXPIRED,
        }
    ),
    IntentStatus.FILLED: frozenset(),
    IntentStatus.CANCELED: frozenset(),
    IntentStatus.REJECTED: frozenset(),
    IntentStatus.EXPIRED: frozenset(),
}

TERMINAL: frozenset[IntentStatus] = frozenset(
    {
        IntentStatus.FILLED,
        IntentStatus.CANCELED,
        IntentStatus.REJECTED,
        IntentStatus.EXPIRED,
    }
)

UNRESOLVED: frozenset[IntentStatus] = frozenset(
    {
        IntentStatus.CREATED,
        IntentStatus.SUBMITTING,
        IntentStatus.SUBMITTED,
        IntentStatus.ACKNOWLEDGED,
        IntentStatus.PARTIALLY_FILLED,
        IntentStatus.CANCEL_PENDING,
        IntentStatus.UNKNOWN,
    }
)
"""Anything not terminal. These block a conflicting entry on the same symbol."""

IN_FLIGHT: frozenset[IntentStatus] = frozenset(
    {
        IntentStatus.SUBMITTING,
        IntentStatus.SUBMITTED,
        IntentStatus.ACKNOWLEDGED,
        IntentStatus.PARTIALLY_FILLED,
        IntentStatus.CANCEL_PENDING,
        IntentStatus.UNKNOWN,
    }
)
"""Reached the broker, or might have. Never re-submit one of these."""


class IllegalTransition(RuntimeError):
    """An order state moved somewhere the lifecycle does not allow."""

    def __init__(self, current: IntentStatus, target: IntentStatus) -> None:
        super().__init__(f"illegal order transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: IntentStatus, target: IntentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: IntentStatus, target: IntentStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


BROKER_STATUS_TO_INTENT: dict[OrderStatus, IntentStatus] = {
    OrderStatus.SUBMITTED: IntentStatus.SUBMITTED,
    OrderStatus.ACCEPTED: IntentStatus.ACKNOWLEDGED,
    OrderStatus.PARTIAL: IntentStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED: IntentStatus.FILLED,
    OrderStatus.CANCELED: IntentStatus.CANCELED,
    OrderStatus.REJECTED: IntentStatus.REJECTED,
    OrderStatus.EXPIRED: IntentStatus.EXPIRED,
}


def intent_status_for(broker_status: OrderStatus, filled_qty: Decimal | None) -> IntentStatus:
    """Normalize broker truth into a domain state.

    A cancelled order that filled part of its quantity is not simply cancelled:
    the fill is a real position, so it maps to PARTIALLY_FILLED and stays
    unresolved until the position is protected.
    """
    mapped = BROKER_STATUS_TO_INTENT.get(broker_status, IntentStatus.UNKNOWN)
    if (
        mapped in {IntentStatus.CANCELED, IntentStatus.EXPIRED}
        and filled_qty is not None
        and filled_qty > 0
    ):
        return IntentStatus.PARTIALLY_FILLED
    return mapped


# ── Model ────────────────────────────────────────────────────────────────────


async def locate_broker_order(broker: BrokerPort, intent: OrderIntent) -> OrderRecord | None:
    """Find the broker order an intent may have created.

    Returns None when the answer is genuinely unknown. Note the asymmetry that
    makes this safe: absence from the open-order book is *not* evidence the
    order never existed, because a filled order is not open either. Callers must
    treat None as "unresolved", never as "nothing happened".
    """
    if intent.broker_order_id:
        try:
            return await broker.get_order(intent.broker_order_id)
        except Exception:
            logger.warning(
                "cannot read broker order %s for intent %s",
                intent.broker_order_id,
                intent.id,
                exc_info=True,
            )
            return None

    if not intent.client_order_id:
        return None
    try:
        return await broker.find_order_by_client_id(intent.client_order_id)
    except Exception:
        logger.warning("cannot look up intent %s by client id", intent.id, exc_info=True)
        return None


# ── Idempotency keys ─────────────────────────────────────────────────────────
#
# Every key is a pure function of durable facts plus an attempt counter derived
# from persisted intents — never from a counter in memory. A retry after a crash
# therefore recomputes the same key and lands on the same intent. A fresh
# attempt only exists once the previous one reached a terminal state, which by
# definition left no live order behind.


def entry_idempotency_key(opportunity_id: UUID, attempt: int) -> str:
    return f"entry:{opportunity_id}:{attempt}"


def exit_idempotency_key(position_id: UUID, attempt: int) -> str:
    """Keyed on the *position*, not the exit card.

    Two exit proposals can be raised for one position — by the position agent
    and by a human — and both mean the same thing: close this. Keying on the
    position makes them collapse onto one broker order instead of two.
    """
    return f"exit:{position_id}:{attempt}"


def emergency_exit_idempotency_key(position_key: str, reason_code: str, generation: int) -> str:
    """Emergency closes are the easiest duplicates to create and the worst to have.

    They fire from failure paths, which are exactly the paths that get retried
    by supervisors and repeated by concurrent workers.
    """
    return f"emergency_exit:{position_key}:{reason_code}:{generation}"


def protection_idempotency_key(position_key: str, generation: int) -> str:
    """The key that makes a protective stop safe to attempt twice.

    A protective stop is external state we do not own: the venue can cancel it,
    an account change can drop it, and on some venues the stop is simulated
    rather than resting, so its presence has to be re-read every pass rather
    than assumed. That means reconciliation will legitimately try to install one
    more than once, and without a key each attempt is a fresh order.

    The generation counter is what distinguishes "install the stop again because
    the last attempt is unresolved" from "install a new stop because the
    position was resized". The first must collapse onto one broker order; the
    second is genuinely a new one.
    """
    return f"protection:{position_key}:{generation}"


def reason_code(text: str) -> str:
    """Collapse free-text failure detail into a stable key fragment.

    An exception message varies between attempts; a key built from one would
    make every retry look like a brand-new emergency close.
    """
    head = (text or "unspecified").split(":", 1)[0].strip().lower()
    slug = "".join(ch if ch.isalnum() else "_" for ch in head).strip("_")
    return (slug or "unspecified")[:32]


class OrderIntent(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    idempotency_key: str
    purpose: IntentPurpose = IntentPurpose.ENTRY
    broker: str
    broker_account_id: str | None = None

    symbol: str
    side: OrderSide
    requested_qty: Decimal
    order_type: OrderType
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None

    strategy_version: str | None = None
    opportunity_id: UUID | None = None
    position_id: UUID | None = None
    related_intent_id: UUID | None = None
    exit_reason: str | None = None
    risk_snapshot: dict[str, object] = Field(default_factory=dict)
    # Entry-only: fail-closed link to ApprovalAdmission. Non-null for purpose=entry.
    approval_admission_record_id: UUID | None = None
    geometry_hash: str | None = None
    request_id: UUID | None = None
    request_fingerprint: str | None = None

    status: IntentStatus = IntentStatus.CREATED
    broker_order_id: str | None = None
    broker_perm_id: str | None = None
    """IB's permId. Survives reconnects and clientId changes; orderId does not."""
    client_order_id: str | None = None
    filled_qty: Decimal = Decimal(0)
    applied_exit_qty: Decimal = Decimal(0)
    """How much of an exit's fill the local book has already absorbed.

    Without this, a reconciliation pass that re-reads the same filled exit would
    reduce the position a second time. It is what makes repeated reconciliation
    idempotent rather than merely harmless-looking.
    """
    average_fill_price: Decimal | None = None
    last_broker_state: str | None = None
    last_error: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_unresolved(self) -> bool:
        return self.status in UNRESOLVED

    @property
    def is_exit(self) -> bool:
        return self.purpose in EXIT_PURPOSES

    @property
    def may_resubmit(self) -> bool:
        """Only an intent that provably never reached the broker may be re-sent."""
        return self.status == IntentStatus.CREATED and self.broker_order_id is None

    @property
    def blocks_new_exposure(self) -> bool:
        """Whether this intent means broker truth for its symbol is unresolved.

        Not the same question as `is_unresolved`, and conflating the two would
        quietly shut the desk down. An entry sitting at `SUBMITTED` is an order
        whose outcome we are still waiting for. A *protective stop* sitting at
        `SUBMITTED` is a stop resting at the venue exactly as intended — the
        steady state of every protected position, lasting days. Counting it as
        unresolved would block new entries in that symbol for as long as the
        position is safe, which is precisely backwards.

        For a protective intent, only `UNKNOWN` is unresolved: we sent a stop
        and cannot say whether the venue has it.
        """
        if not self.is_unresolved:
            return False
        if self.purpose is IntentPurpose.PROTECTIVE_EXIT:
            return self.status is IntentStatus.UNKNOWN
        return True


def unresolved_exit_for(
    intents: list[OrderIntent],
    *,
    symbol: str,
    position_id: UUID | None,
    purposes: frozenset[IntentPurpose] = EXIT_PURPOSES,
) -> OrderIntent | None:
    """Find an exit already in flight for this position.

    Matching falls back to the symbol when no position id is known, because an
    emergency flatten triggered before a ledger row exists still must not be
    able to fire twice.
    """
    ticker = symbol.upper()
    for intent in intents:
        if intent.purpose not in purposes or not intent.is_unresolved:
            continue
        if position_id is not None and intent.position_id == position_id:
            return intent
        if position_id is None and intent.symbol.upper() == ticker:
            return intent
    return None
