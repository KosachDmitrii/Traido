"""A news feed that could not be read is not a clear news feed.

`propose_trade` vetoes on `news.sentiment == "negative"`, so headlines are a
gate rather than decoration. Before this, the two ways of failing to read them
disagreed: a vendor outage killed the whole symbol, while a *missing API key*
returned a neutral 50 and let the veto silently not fire. The second is the
shape the earnings calendar had before it was closed — absence of a read
presented as absence of bad news.

Both paths now report why the check did not happen, and the risk engine refuses
on it, exactly as it does for the calendar. `require_news_check=false` is the
only way past, and it is recorded on the decision taken under it.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from core.enums import NewsCheck, RiskVerdict, TradeAction
from core.schemas import PortfolioSnapshot, RiskLimits, TradeCandidate
from risk.risk_engine import RiskContext, RiskEngine
from tests.support import CLEARED_EARNINGS


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="MU",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(120),
        risk_reward=4.0,
        reasons=["fixture"],
        strategy_version="test-v1",
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )


def _context(news: NewsCheck) -> RiskContext:
    return RiskContext(
        earnings=CLEARED_EARNINGS.earnings,
        next_earnings=CLEARED_EARNINGS.next_earnings,
        last_earnings=CLEARED_EARNINGS.last_earnings,
        news=news,
        sector=CLEARED_EARNINGS.sector,
        sector_check=CLEARED_EARNINGS.sector_check,
    )


class TestTheAgentReportsWhyItCouldNotRead:
    @pytest.mark.asyncio
    async def test_no_key_is_not_configured_rather_than_neutral(self) -> None:
        from agents.news.agent import assess_news

        assessment = await assess_news("MU", None)

        assert assessment.status is NewsCheck.NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_a_vendor_outage_is_unavailable_rather_than_an_exception(self) -> None:
        """The 503 that started this. The symbol must survive it to be refused.

        Raising killed the whole pipeline for the symbol, which reported as
        `no_candidate` — indistinguishable on the funnel from a setup that
        simply was not there.
        """
        from agents.news.agent import assess_news

        async def unavailable(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        assessment = await assess_news("MU", "k" * 20, transport=httpx.MockTransport(unavailable))

        assert assessment.status is NewsCheck.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_successful_read_is_checked(self) -> None:
        from agents.news.agent import assess_news

        async def ok(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"headline": "Company beats on revenue"}])

        assessment = await assess_news("MU", "k" * 20, transport=httpx.MockTransport(ok))

        assert assessment.status is NewsCheck.CHECKED
        assert assessment.headlines


class TestTheRiskEngineRefusesAnUnreadFeed:
    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (NewsCheck.NOT_CONFIGURED, "NEWS_NOT_CONFIGURED"),
            (NewsCheck.UNAVAILABLE, "NEWS_UNAVAILABLE"),
            (NewsCheck.NOT_CHECKED, "NEWS_UNVERIFIED"),
        ],
    )
    def test_each_way_of_not_reading_is_refused_by_its_own_name(
        self, status: NewsCheck, reason: str
    ) -> None:
        """Kept apart because the operator's response differs.

        A missing key is fixed in a minute; an outage clears on its own.
        """
        decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=_context(status))

        assert decision.verdict is RiskVerdict.REJECT
        assert reason in decision.reasons

    def test_a_read_feed_passes(self) -> None:
        decision = RiskEngine().evaluate(
            _candidate(), _portfolio(), context=_context(NewsCheck.CHECKED)
        )

        assert decision.verdict is not RiskVerdict.REJECT, decision.reasons

    def test_the_default_context_refuses_rather_than_assuming_a_read(self) -> None:
        """A caller that forgets to pass news must not thereby clear the gate."""
        decision = RiskEngine().evaluate(
            _candidate(),
            _portfolio(),
            context=RiskContext(
                earnings=CLEARED_EARNINGS.earnings,
                next_earnings=CLEARED_EARNINGS.next_earnings,
                last_earnings=CLEARED_EARNINGS.last_earnings,
            ),
        )

        assert decision.verdict is RiskVerdict.REJECT
        assert "NEWS_UNVERIFIED" in decision.reasons

    def test_the_check_can_be_waived_deliberately(self) -> None:
        limits = RiskLimits(require_news_check=False)

        decision = RiskEngine(limits).evaluate(
            _candidate(), _portfolio(), context=_context(NewsCheck.UNAVAILABLE)
        )

        assert decision.verdict is not RiskVerdict.REJECT, decision.reasons

    def test_a_waiver_is_recorded_on_the_decision(self) -> None:
        """A trade taken without a news check must be identifiable afterwards."""
        limits = RiskLimits(require_news_check=False)

        decision = RiskEngine(limits).evaluate(
            _candidate(), _portfolio(), context=_context(NewsCheck.UNAVAILABLE)
        )

        assert decision.limits_applied.require_news_check is False
