"""
Deterministic Risk Engine — NO LLM.

REJECT always wins. Kill switch cannot be bypassed by any agent.

The engine answers one question: given everything already on the book and
everything known about the calendar, is this trade allowed and at what size?
Checks are ordered cheapest-and-most-fatal first, and every rejection reason is
a stable machine-readable code so the desk UI and the audit log agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from core.clock import market_date
from core.enums import EarningsCheck, NewsCheck, RiskVerdict, SectorCheck, TradeAction
from core.schemas import PortfolioSnapshot, RiskDecision, RiskLimits, TradeCandidate
from quant.correlation import CorrelationMatrix, check_concentration
from risk.position_sizing import size_long_shares


@dataclass(frozen=True)
class RiskContext:
    """
    Everything outside the candidate and the portfolio snapshot that can veto a trade.

    All fields are optional, and the engine never invents data it was not given.
    Where the fields differ is in what silence means. For correlation and sector
    exposure, missing data means the check does not run: a book we cannot see is
    not evidence of concentration. For the earnings calendar it is the reverse —
    silence is refused, because a print we failed to look up is exactly as
    ruinous as one we looked up and ignored.
    """

    open_symbols: list[str] = field(default_factory=list)
    correlations: CorrelationMatrix | None = None

    sector: str | None = None
    """Sector of the candidate. Only ever a real one — never `"unknown"`."""
    sector_check: SectorCheck = SectorCheck.NOT_CHECKED
    """Whether `sector` above was established. Defaults to "nobody looked".

    Strict by default for the reason `earnings` and `news` are: the alternative
    silently promotes a caller who never established a sector into one who
    found the name unconcentrated.
    """
    sector_exposure: dict[str, Decimal] = field(default_factory=dict)
    """Current notional exposure per sector. Classified names only."""
    unclassified_exposure: Decimal = Decimal(0)
    """Open notional whose sector could not be established.

    Held apart rather than dropped or bucketed under a name. A position we
    cannot classify is not evidence of a *different* sector — it is exposure
    that might belong to whichever sector we are about to add to, and the engine
    charges it against that sector for exactly that reason.

    It is not hypothetical: a name bought before its sector was mapped, or under
    a waiver, sits on the book afterwards. Leaving it out understates every
    sector it might belong to, which is the same hole as the candidate's, one
    step later.
    """

    earnings: EarningsCheck = EarningsCheck.NOT_CHECKED
    """Whether the calendar below was actually read. Defaults to "nobody looked".

    Strict by default because the alternative default silently converts every
    caller that forgot to fetch a calendar into a caller that cleared one.
    """
    next_earnings: date | None = None
    last_earnings: date | None = None

    news: NewsCheck = NewsCheck.NOT_CHECKED
    """Whether the headlines were read for this symbol. Defaults to "nobody looked".

    Strict for the same reason as `earnings`: the strategy vetoes on negative
    sentiment, so a feed we could not read is a veto that cannot fire, and a
    caller who forgot to fetch one must not be silently promoted to a caller
    who cleared it.
    """

    regime_tradable: bool | None = None
    """False when the market regime agent says long setups should stand down."""

    unresolved_symbols: frozenset[str] = frozenset()
    """Symbols whose broker state is unresolved. No new exposure until it is."""

    now: datetime | None = None

    def today(self) -> date:
        """The exchange's day, not the server's.

        The earnings windows are counted against a calendar published in
        exchange-local terms, so the day they are counted from has to be the
        same one. A UTC date drifts ahead of it every evening.
        """
        return market_date(self.now)


_UNVERIFIED_REASON = {
    # Split by what the operator would have to do about it. A missing key is
    # fixed in a minute and then every symbol clears; a vendor outage resolves
    # itself and is not worth chasing. Collapsing them into one code would put
    # both behind the same undifferentiated count on the funnel.
    EarningsCheck.NOT_CONFIGURED: "EARNINGS_CALENDAR_NOT_CONFIGURED",
    EarningsCheck.UNAVAILABLE: "EARNINGS_CALENDAR_UNAVAILABLE",
    EarningsCheck.NOT_CHECKED: "EARNINGS_UNVERIFIED",
}

_UNREAD_NEWS_REASON = {
    NewsCheck.NOT_CONFIGURED: "NEWS_NOT_CONFIGURED",
    NewsCheck.UNAVAILABLE: "NEWS_UNAVAILABLE",
    NewsCheck.NOT_CHECKED: "NEWS_UNVERIFIED",
}

_UNKNOWN_SECTOR_REASON = {
    # Same split as earnings/news: one is a name to map, one is a missing key,
    # one is a vendor outage, one is a caller that never asked.
    SectorCheck.UNCLASSIFIED: "SECTOR_UNCLASSIFIED",
    SectorCheck.NOT_CONFIGURED: "SECTOR_NOT_CONFIGURED",
    SectorCheck.UNAVAILABLE: "SECTOR_UNAVAILABLE",
    SectorCheck.NOT_CHECKED: "SECTOR_UNVERIFIED",
}


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        if self.limits.allow_leverage or self.limits.allow_short or self.limits.allow_options:
            raise ValueError("V1 RiskEngine forbids leverage, shorts, and options")

    def evaluate(
        self,
        candidate: TradeCandidate,
        portfolio: PortfolioSnapshot,
        *,
        candidate_id: UUID | None = None,
        context: RiskContext | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        ctx = context or RiskContext()

        if portfolio.kill_switch:
            return self._reject(["KILL_SWITCH"], portfolio, candidate_id, ctx.earnings)

        if candidate.action != TradeAction.BUY:
            return self._reject(["V1_LONG_ONLY"], portfolio, candidate_id, ctx.earnings)

        if candidate.stop >= candidate.entry or candidate.target <= candidate.entry:
            return self._reject(["INVALID_GEOMETRY"], portfolio, candidate_id, ctx.earnings)

        equity = portfolio.equity
        if equity <= 0:
            return self._reject(["NON_POSITIVE_EQUITY"], portfolio, candidate_id, ctx.earnings)

        # Daily / weekly loss & drawdown
        day_loss_pct = float((-portfolio.day_pnl / equity) * 100) if portfolio.day_pnl < 0 else 0.0
        week_loss_pct = (
            float((-portfolio.week_pnl / equity) * 100) if portfolio.week_pnl < 0 else 0.0
        )
        if day_loss_pct >= self.limits.max_daily_loss_pct:
            reasons.append("MAX_DAILY_LOSS")
        if week_loss_pct >= self.limits.max_weekly_loss_pct:
            reasons.append("MAX_WEEKLY_LOSS")
        if portfolio.drawdown_pct >= self.limits.max_portfolio_drawdown_pct:
            reasons.append("MAX_PORTFOLIO_DRAWDOWN")

        if portfolio.open_positions >= self.limits.max_open_positions:
            reasons.append("MAX_OPEN_POSITIONS")

        if candidate.symbol.upper() in ctx.unresolved_symbols:
            # We do not know what we already own here. Adding to it is the one
            # action guaranteed to make the ambiguity worse.
            reasons.append("UNRESOLVED_BROKER_STATE")

        reasons.extend(self._event_risk(candidate, ctx))
        reasons.extend(self._concentration(candidate, ctx))

        if ctx.regime_tradable is not True:
            reasons.append(
                "REGIME_NOT_TRADABLE" if ctx.regime_tradable is False else "REGIME_MISSING"
            )

        risk_per_share = candidate.entry - candidate.stop
        if risk_per_share <= 0:
            return self._reject(
                ["NON_POSITIVE_RISK_PER_SHARE"], portfolio, candidate_id, ctx.earnings
            )

        qty, max_loss = size_long_shares(
            equity=equity,
            entry=candidate.entry,
            stop=candidate.stop,
            risk_pct=self.limits.max_risk_per_trade_pct,
            max_position_pct=self.limits.max_position_pct,
            cash=portfolio.cash,
        )
        if qty <= 0:
            reasons.append("SIZE_ZERO")

        # Exposure: reject if adding this name would exceed soft book concentration
        notional = qty * candidate.entry
        if equity > 0 and float(notional / equity) * 100 > self.limits.max_position_pct + 1e-9:
            reasons.append("MAX_POSITION_PCT")
        # Cap total open exposure at 5x single-name cap (book heat)
        new_exposure = portfolio.open_exposure + notional
        max_book_pct = self.limits.max_position_pct * 5
        if equity > 0 and float(new_exposure / equity) * 100 > max_book_pct + 1e-9:
            reasons.append("MAX_BOOK_EXPOSURE")

        if ctx.sector:
            # Unclassified book exposure is charged against whichever sector is
            # being added to. It is the only reading that cannot understate: the
            # shares are somewhere, and assuming they are here is the assumption
            # that refuses rather than the one that permits.
            current = ctx.sector_exposure.get(ctx.sector, Decimal(0)) + ctx.unclassified_exposure
            sector_pct = float((current + notional) / equity) * 100
            if sector_pct > self.limits.max_sector_pct + 1e-9:
                reasons.append("MAX_SECTOR_EXPOSURE")

        if float(max_loss / equity) * 100 > self.limits.max_risk_per_trade_pct + 1e-6:
            reasons.append("MAX_RISK_PER_TRADE")

        if reasons:
            return self._reject(reasons, portfolio, candidate_id, ctx.earnings)

        return RiskDecision(
            verdict=RiskVerdict.PASS,
            reasons=["RISK_OK"],
            sized_qty=qty,
            max_loss_usd=max_loss,
            limits_applied=self.limits,
            portfolio=portfolio,
            candidate_id=candidate_id,
            earnings_check=ctx.earnings,
        )

    def _event_risk(self, candidate: TradeCandidate, ctx: RiskContext) -> list[str]:
        """Refuse to hold through a scheduled binary event — or through an unread
        calendar, which is the same exposure with none of the warning."""
        reasons: list[str] = []
        today = ctx.today()

        if self.limits.require_earnings_check and ctx.earnings is not EarningsCheck.CHECKED:
            # Not "no print is scheduled" — we could not see the calendar at all.
            reasons.append(_UNVERIFIED_REASON[ctx.earnings])

        if self.limits.require_news_check and ctx.news is not NewsCheck.CHECKED:
            # Not "the headlines were clean" — we never read them. The strategy's
            # negative-sentiment veto cannot fire on a feed nobody could see.
            reasons.append(_UNREAD_NEWS_REASON[ctx.news])

        if self.limits.require_sector_check and ctx.sector_check is not SectorCheck.CHECKED:
            # Not "this name concentrates nothing" — we could not say where it
            # belongs, and the cap above only binds a name we can place.
            reasons.append(_UNKNOWN_SECTOR_REASON[ctx.sector_check])

        if ctx.next_earnings is not None:
            days_until = (ctx.next_earnings - today).days
            if 0 <= days_until <= self.limits.block_days_before_earnings:
                reasons.append("EARNINGS_IMMINENT")

        if ctx.last_earnings is not None:
            days_since = (today - ctx.last_earnings).days
            if 0 <= days_since <= self.limits.block_days_after_earnings:
                reasons.append("EARNINGS_JUST_REPORTED")

        del candidate
        return reasons

    def _concentration(self, candidate: TradeCandidate, ctx: RiskContext) -> list[str]:
        """Refuse a trade that duplicates exposure already on the book."""
        if ctx.correlations is None or not ctx.open_symbols:
            return []
        check = check_concentration(
            candidate.symbol,
            ctx.open_symbols,
            ctx.correlations,
            max_pair_correlation=self.limits.max_correlation,
            min_effective_positions=self.limits.min_effective_positions,
        )
        return list(check.breaches)

    def _reject(
        self,
        reasons: list[str],
        portfolio: PortfolioSnapshot,
        candidate_id: UUID | None,
        earnings: EarningsCheck = EarningsCheck.NOT_CHECKED,
    ) -> RiskDecision:
        return RiskDecision(
            verdict=RiskVerdict.REJECT,
            reasons=reasons,
            sized_qty=None,
            max_loss_usd=None,
            limits_applied=self.limits,
            portfolio=portfolio,
            candidate_id=candidate_id,
            earnings_check=earnings,
        )
