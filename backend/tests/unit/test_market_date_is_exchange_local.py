"""An earnings date is a date at the exchange, so "today" has to be one too.

Finnhub publishes a print for a trading day. The engine counts the blackout
window from `today`, and the provider splits the calendar into next and last at
`today`. Both used `datetime.now(UTC).date()`, which is the exchange's day for
twenty hours out of twenty-four and the day after it for the other four: from
20:00 ET the UTC calendar has turned over and the exchange's has not.

Every evaluation in that window was a day off in the direction of looking closer
to a print than it was, which is why nothing lost money over it. Direction is
not correctness, though — the split in the provider is not conservative in the
same direction, and it can file a print scheduled for tonight under `last_date`,
where the window is one day rather than three.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.clock import market_date
from core.enums import SectorCheck,  EarningsCheck, NewsCheck, RiskVerdict, TradeAction
from core.schemas import PortfolioSnapshot, TradeCandidate
from market_data.providers.earnings import parse_earnings_payload
from risk.risk_engine import RiskContext, RiskEngine

# 21:30 in New York, and already tomorrow in UTC. Every assertion below turns on
# these being two different days.
_EVENING_ET = datetime(2026, 3, 10, 1, 30, tzinfo=UTC)
_ET_DAY = date(2026, 3, 9)
_UTC_DAY = date(2026, 3, 10)


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(115),
        confidence=0.8,
        risk_reward=3.0,
        reasons=["exchange-local date test"],
        strategy_version="test@1",
        pipeline_run_id=uuid4(),
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(100000),
        cash=Decimal(100000),
        buying_power=Decimal(100000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )


def _verdict(next_earnings: date | None) -> tuple[RiskVerdict, list[str]]:
    decision = RiskEngine().evaluate(
        _candidate(),
        _portfolio(),
        context=RiskContext(
            news=NewsCheck.CHECKED,
            earnings=EarningsCheck.CHECKED,
            sector_check=SectorCheck.CHECKED,
            sector="technology",
            next_earnings=next_earnings,
            now=_EVENING_ET,
        ),
    )
    return decision.verdict, decision.reasons


def test_the_market_date_is_the_day_at_the_exchange() -> None:
    assert market_date(_EVENING_ET) == _ET_DAY
    assert _EVENING_ET.date() == _UTC_DAY, "the fixture is pointless if these agree"
    assert RiskContext(news=NewsCheck.CHECKED, now=_EVENING_ET).today() == _ET_DAY


def test_the_blackout_window_is_counted_from_the_exchange_day() -> None:
    """One day past a three-day window — a UTC "today" pulls it back inside."""
    window = RiskEngine().limits.block_days_before_earnings
    verdict, reasons = _verdict(_ET_DAY + timedelta(days=window + 1))

    assert "EARNINGS_IMMINENT" not in reasons
    assert verdict is RiskVerdict.PASS

    inside, _ = _verdict(_ET_DAY + timedelta(days=window))
    assert inside is RiskVerdict.REJECT, "the window itself still has to bite"


def test_tonights_print_is_still_ahead_of_us_not_behind() -> None:
    """Where the UTC skew stops being merely conservative.

    A print dated today has not happened yet — the position would be carried
    into it. Split at the UTC day it lands in `last_date` instead, and the
    engine sees no upcoming print at all.
    """
    payload = {"earningsCalendar": [{"date": _ET_DAY.isoformat()}]}

    tonight = parse_earnings_payload("AAPL", payload, market_date(_EVENING_ET))
    assert tonight.next_date == _ET_DAY
    assert tonight.last_date is None
    assert "EARNINGS_IMMINENT" in _verdict(tonight.next_date)[1]

    skewed = parse_earnings_payload("AAPL", payload, _EVENING_ET.date())
    assert skewed.next_date is None, "the bug this test exists to keep out"
