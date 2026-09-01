"""The session the header shows must be the session the gate enforces.

The desk refuses new entries outside the regular session. Putting that on screen
is only worth doing if it is the same answer the RTH gate will give when the
operator clicks BUY — a header that says "market open" against a gate that
rejects is worse than no header, because it moves the surprise later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from api.routes import desk as desk_routes
from trading.gates import check_rth

ET = ZoneInfo("America/New_York")


def _at(moment: datetime, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build the payload as if `moment` were now."""

    class _Frozen:
        @staticmethod
        def now(tz: object = None) -> datetime:
            return moment.astimezone(tz) if tz else moment

    monkeypatch.setattr(desk_routes, "datetime", _Frozen)
    return desk_routes._session_payload()


# Wednesdays and weekend days off the 2026 calendar. March is already on
# daylight time, so ET is UTC-4 here.
CASES = [
    (datetime(2026, 3, 11, 13, 0, tzinfo=UTC), "premarket", False),  # 09:00 ET
    (datetime(2026, 3, 11, 15, 0, tzinfo=UTC), "regular", True),  # 11:00 ET
    (datetime(2026, 3, 11, 21, 30, tzinfo=UTC), "after_hours", False),  # 17:30 ET
    (datetime(2026, 3, 14, 15, 0, tzinfo=UTC), "closed_weekend", False),  # Saturday
    (datetime(2026, 12, 25, 15, 0, tzinfo=UTC), "closed_holiday", False),  # Christmas
]


@pytest.mark.parametrize(("moment", "phase", "allowed"), CASES)
def test_the_phase_and_its_consequence_are_reported(
    moment: datetime, phase: str, allowed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _at(moment, monkeypatch)
    assert payload["phase"] == phase
    assert payload["entries_allowed"] is allowed


@pytest.mark.parametrize(("moment", "phase", "allowed"), CASES)
def test_the_header_never_disagrees_with_the_rth_gate(
    moment: datetime, phase: str, allowed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both read the same calendar today. This is what keeps it that way."""
    payload = _at(moment, monkeypatch)
    assert payload["entries_allowed"] is check_rth(moment).passed


def test_an_early_close_is_read_rather_than_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Friday after Thanksgiving 2026 ends at 13:00 ET, not 16:00.

    A hardcoded 16:00 would tell the operator entries are open for three hours
    after the gate has started refusing them.
    """
    midday = datetime(2026, 11, 27, 17, 0, tzinfo=UTC)  # 12:00 ET
    payload = _at(midday, monkeypatch)
    assert payload["phase"] == "regular"
    assert payload["closes_at"] == "13:00"

    after = datetime(2026, 11, 27, 19, 0, tzinfo=UTC)  # 14:00 ET
    assert _at(after, monkeypatch)["entries_allowed"] is False


def test_the_clock_is_reported_in_exchange_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shown next to the phase, so it has to be the clock the phase was judged
    against — not the browser's, and not UTC."""
    payload = _at(datetime(2026, 3, 11, 15, 0, tzinfo=UTC), monkeypatch)
    assert payload["et_time"] == "11:00"


def test_the_date_is_the_exchanges_even_when_utc_has_moved_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """21:00 in New York is already tomorrow in UTC, and later still in Moscow.

    The header shows a bare `08:33` that an operator abroad reads against their
    own wall clock. Labelling the zone without the date only half-fixes that:
    during the evening session the day itself differs, which is the same
    off-by-one `core.clock.market_date` exists to keep out of the code.
    """
    evening = datetime(2026, 3, 12, 1, 0, tzinfo=UTC)  # 21:00 ET, Wednesday
    payload = _at(evening, monkeypatch)

    assert payload["et_date"] == "Wed Mar 11"
    assert payload["et_time"] == "21:00"


def test_the_clock_reaches_the_browser_instead_of_freezing_behind_a_304() -> None:
    """The desk answers 304 whenever its fingerprint is unchanged. Leave the
    session out of that fingerprint and the header clock stops at whatever
    minute the last real change happened."""
    base: dict = {"activity": {"agents": []}, "session": {"et_time": "11:00", "phase": "regular"}}
    later = {**base, "session": {"et_time": "11:01", "phase": "regular"}}

    assert desk_routes._etag_for(base) != desk_routes._etag_for(later)
