"""What day it is on the exchange.

Every date this system reasons about is a US market date: an earnings print is
scheduled for a trading day, and the windows around it are counted in trading
days. `datetime.now(UTC).date()` is a different quantity, and it is a different
quantity for four hours out of every twenty-four — after 20:00 ET the UTC
calendar has already turned over while the exchange has not.

The skew is small and it is not symmetric: it makes a print look nearer than it
is, so it errs toward refusing a trade. That is why it was not a live incident.
It is still the wrong number, and code that compares an exchange calendar date
against a UTC one is a bug waiting for the day the windows are widened or the
comparison is reused somewhere the safe direction is reversed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
"""The exchange's clock. Handles DST, so this is not a fixed UTC offset."""


def market_date(now: datetime | None = None) -> date:
    """The calendar day at the exchange, for a moment given in any timezone."""
    return (now or datetime.now(UTC)).astimezone(ET).date()
