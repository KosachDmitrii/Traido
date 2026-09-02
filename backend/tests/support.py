"""Shared scaffolding for tests that need a trade to reach execution.

A `RiskContext` says nothing about the earnings calendar by default, and the
engine refuses an entry whose calendar was never read. That is the point of the
default — but a test about order lifecycle or idempotency is not a test about
event risk, and restating the calendar at every call site would bury what each
one is actually asserting. So it is stated once, here, by name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.enums import (
    EarningsCheck,
    EntryDecision,
    InstrumentThesis,
    NewsCheck,
    SectorCheck,
    SetupType,
    TargetReachabilityClass,
    Timeframe,
    TradeAction,
)
from core.schemas import AdmissionSnapshot, Bar, FeatureSnapshot, Quote, TradeCandidate
from risk.risk_engine import RiskContext

CLEARED_EARNINGS = RiskContext(
    earnings=EarningsCheck.CHECKED,
    news=NewsCheck.CHECKED,
    sector="technology",
    sector_check=SectorCheck.CHECKED,
    regime_tradable=True,
)
"""Vendor checks and sector were established — what a live entry has cleared.

The calendar was read and no print is near; the headlines were read and nothing
in them vetoes; the name sits in a known sector. Tests about order lifecycle or
sizing are not tests about those gates, but they still have to clear all three
to reach their subject.
"""


class LiquidMarketData:
    """A symbol that comfortably clears the liquidity gate.

    The same reasoning as `CLEARED_EARNINGS`, for the other gate that refuses an
    entry it could not measure: a test about order lifecycle is not a test about
    spread, but it still has to get past the spread check to reach its subject.

    The quote is stamped on whatever clock the execution service is reading,
    which the suite freezes to a mid-session instant. Stamping it from the wall
    clock instead makes every entry test fail as `QUOTE_STALE` — correctly, and
    for a reason that has nothing to do with what the test is asserting.
    """

    def __init__(self, *, price: float = 100.0, volume: float = 5_000_000.0) -> None:
        self.price = price
        self.volume = volume

    @staticmethod
    def _now() -> datetime:
        from trading import execution

        return execution._utcnow()

    async def get_quote(self, symbol: str) -> Quote | None:
        half = Decimal(str(self.price)) * Decimal("0.0001")
        return Quote(
            symbol=symbol,
            bid=Decimal(str(self.price)) - half,
            ask=Decimal(str(self.price)) + half,
            ts=self._now(),
            source="synthetic",
        )

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        # Honour the caller's end so sector/admission freshness checks agree with
        # the evaluation clock (wall-clock _utcnow can race a few ms ahead).
        now = end
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        step = timedelta(hours=1) if timeframe != Timeframe.D1 else timedelta(days=1)
        bars: list[Bar] = []
        for i in range(60):
            ts = now - step * (59 - i)
            # Mild pullback into self.price so entry geometry / chase gates agree
            # with the synthetic quote stamped at self.price.
            px = self.price * (1.0 + 0.002 * (59 - i) / 59.0)
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts,
                    open=px,
                    high=px * 1.005,
                    low=px * 0.995,
                    close=px,
                    volume=self.volume,
                    source="synthetic",
                )
            )
        return bars

    async def get_last_price(self, symbol: str) -> float:
        return self.price


def liquid_market_data(**kwargs: float) -> LiquidMarketData:
    """A market-data port for tests whose subject is not liquidity."""
    return LiquidMarketData(**kwargs)  # type: ignore[arg-type]


_ENTRY_BREAKDOWN = {
    "price_location": 75,
    "vwap_location": 70,
    "atr_extension": 72,
    "pullback_quality": 74,
    "remaining_reward": 76,
    "support_structure": 78,
    "resistance_structure": 70,
    "short_term_momentum": 68,
    "volume_confirmation": 71,
    "market_alignment": 73,
    "signal_drift": 80,
}


def admission_ready_candidate(
    *,
    symbol: str = "AAPL",
    entry: float = 100.0,
    stop: float = 95.0,
    target: float = 115.0,
    market_label: str = "risk_on",
    strategy_version: str = "test@1",
    target_reachability: TargetReachabilityClass = TargetReachabilityClass.REALISTIC,
    target_model: str = "structure",
) -> TradeCandidate:
    """Candidate with real admission metadata for the approval capital path.

    Does not invent reachability. Does not silently lift the caller's target.
    Happy-path callers should pass a target that already clears effective R:R.
    """
    from uuid import uuid4

    risk = max(entry - stop, entry * 0.005)
    rr = (target - entry) / risk if risk > 0 else 2.0
    snap = AdmissionSnapshot(
        price_at_creation=entry,
        atr_at_creation=risk,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=80,
        entry_quality_at_creation=75,
        stop_at_creation=stop,
        target_at_creation=target,
        effective_rr_at_creation=rr,
        structural_source="nearest_support",
        structural_level=stop,
        stop_model="structure",
        entry_zone_low=entry * 0.995,
        entry_zone_high=entry * 1.005,
    )
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal(str(entry)),
        stop=Decimal(str(stop)),
        target=Decimal(str(target)),
        risk_reward=round(rr, 2),
        reasons=["test admission-ready"],
        strategy_version=strategy_version,
        pipeline_run_id=uuid4(),
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=80,
        entry_quality=75,
        entry_quality_breakdown=dict(_ENTRY_BREAKDOWN),
        setup_quality_breakdown={
            "trend_structure": 80,
            "impulse_quality": 78,
            "retracement_structure": 76,
            "volume_participation": 74,
            "support_structure": 80,
            "market_alignment": 72,
            "catalyst": 70,
            "liquidity": 85,
        },
        market_label=market_label,
        target_model=target_model,
        target_reachability=target_reachability,
        admission_version="admission@1.1.0",
        admission_snapshot=snap.model_dump(mode="json"),
        entry_zone_low=Decimal(str(round(entry * 0.995, 4))),
        entry_zone_high=Decimal(str(round(entry * 1.005, 4))),
        exec_timeframe=Timeframe.H1,
        signal_price=Decimal(str(entry)),
    )


def ensure_admission_ready(candidate: TradeCandidate) -> TradeCandidate:
    """Attach missing admission metadata without changing an already-admitted geometry.

    When inventing metadata from scratch for bare test candidates, the target is
    raised only enough to clear the default effective-RR floor so lifecycle tests
    reach their subject. Callers that already stamped admission facts keep their
    exact geometry — including UNREALISTIC and sub-threshold R:R.
    """
    if (
        candidate.admission_version
        and candidate.admission_snapshot
        and candidate.target_model
        and candidate.target_reachability is not None
        and candidate.entry_decision is not None
        and candidate.thesis is not None
        and candidate.entry_quality_breakdown
    ):
        return candidate
    risk = float(candidate.entry) - float(candidate.stop)
    target = float(candidate.target)
    if risk > 0:
        target = max(target, float(candidate.entry) + risk * 2.6)
    base = admission_ready_candidate(
        symbol=candidate.symbol,
        entry=float(candidate.entry),
        stop=float(candidate.stop),
        target=target,
        market_label=candidate.market_label or "risk_on",
        strategy_version=candidate.strategy_version,
        target_reachability=candidate.target_reachability or TargetReachabilityClass.REALISTIC,
        target_model=candidate.target_model or "structure",
    )
    return base.model_copy(
        update={
            "pipeline_run_id": candidate.pipeline_run_id,
            "reasons": candidate.reasons,
            "confidence": candidate.confidence,
            "target_reachability": candidate.target_reachability or base.target_reachability,
            "target_model": candidate.target_model or base.target_model,
        }
    )


def feature_snap(symbol: str = "AAPL", close: float = 100.0, atr: float = 2.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        timeframe=Timeframe.H1,
        computed_at=datetime.now(UTC),
        indicators={"close": close, "atr_14": atr, "sma_20": close - 1, "vwap": close - 0.5},
        candlestick_patterns={},
        chart_patterns={},
        support=[Decimal(str(close - atr * 2))],
        resistance=[Decimal(str(close + atr * 4))],
    )


def fresh_quote(symbol: str, bid: float, ask: float, *, age_sec: float = 0.0) -> Quote:
    return Quote(
        symbol=symbol,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC) - timedelta(seconds=age_sec),
        source="test",
    )
