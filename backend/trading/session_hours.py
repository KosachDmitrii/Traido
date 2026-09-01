"""US equity session calendar.

Deterministic on purpose: no vendor calendar call sits between a decision and an
order. NYSE holidays follow fixed rules, so they are computed rather than
fetched, which means the same input always produces the same answer and the
gate can be unit-tested without a network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from core.clock import ET

__all__ = [
    "ET",
    "SessionPhase",
    "fill_wait_seconds",
    "session_close",
    "session_phase",
    "us_equity_rth_open",
]

_OPEN = time(9, 30)
_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)


class SessionPhase(StrEnum):
    CLOSED_WEEKEND = "closed_weekend"
    CLOSED_HOLIDAY = "closed_holiday"
    PREMARKET = "premarket"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm — Good Friday is the only movable holiday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """A fixed-date holiday on a weekend is observed on the adjacent weekday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def market_holidays(year: int) -> frozenset[date]:
    """NYSE full-day closures for a calendar year."""
    return frozenset(
        {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _easter(year) - timedelta(days=2),  # Good Friday
            _last_weekday(year, 5, 0),  # Memorial Day
            _observed(date(year, 6, 19)),  # Juneteenth
            _observed(date(year, 7, 4)),  # Independence Day
            _nth_weekday(year, 9, 0, 1),  # Labor Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving
            _observed(date(year, 12, 25)),  # Christmas
        }
    )


def early_close_days(year: int) -> frozenset[date]:
    """Sessions that end at 13:00 ET instead of 16:00 ET."""
    days: set[date] = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}  # day after Thanksgiving
    july_3 = date(year, 7, 3)
    if july_3.weekday() < 5 and _observed(date(year, 7, 4)) != july_3:
        days.add(july_3)
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5:
        days.add(christmas_eve)
    return frozenset(days)


def is_market_holiday(day: date) -> bool:
    return day in market_holidays(day.year)


def session_close(day: date) -> time:
    return _EARLY_CLOSE if day in early_close_days(day.year) else _CLOSE


def session_phase(now: datetime | None = None) -> SessionPhase:
    """Classify a moment against the US equity calendar."""
    ts = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    local = ts.astimezone(ET)
    day = local.date()

    if local.weekday() >= 5:
        return SessionPhase.CLOSED_WEEKEND
    if is_market_holiday(day):
        return SessionPhase.CLOSED_HOLIDAY

    clock = local.time()
    if clock < _OPEN:
        return SessionPhase.PREMARKET
    if clock < session_close(day):
        return SessionPhase.REGULAR
    return SessionPhase.AFTER_HOURS


def us_equity_rth_open(now: datetime | None = None) -> bool:
    """True during the regular session, holidays and early closes included."""
    return session_phase(now) is SessionPhase.REGULAR


def fill_wait_seconds(*, in_session: bool | None = None) -> float:
    """
    Keep under typical Next.js rewrite proxy patience (~30s).
    Outside RTH limits almost never fill — fail fast and cancel.
    """
    open_now = us_equity_rth_open() if in_session is None else in_session
    return 18.0 if open_now else 2.5
