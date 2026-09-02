"""Shared scaffolding for tests that need a trade to reach execution.

A `RiskContext` says nothing about the earnings calendar by default, and the
engine refuses an entry whose calendar was never read. That is the point of the
default — but a test about order lifecycle or idempotency is not a test about
event risk, and restating the calendar at every call site would bury what each
one is actually asserting. So it is stated once, here, by name.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from core.enums import EarningsCheck, NewsCheck, SectorCheck, Timeframe
from core.schemas import Bar, Quote, TradeCandidate
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
        # Last bar must be fresh relative to the frozen execution clock.
        now = self._now()
        step = timedelta(hours=1) if timeframe != Timeframe.D1 else timedelta(days=1)
        bars: list[Bar] = []
        for i in range(60):
            ts = now - step * (59 - i)
            # Mild downtrend into support so feature engine finds structure.
            px = self.price * (1.0 - 0.001 * (59 - i) / 59.0)
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


def admission_ready_candidate(
    *,
    symbol: str = "AAPL",
    entry: float = 100.0,
    stop: float = 95.0,
    target: float = 115.0,
    market_label: str = "neutral",
    strategy_version: str = "test@1",
) -> TradeCandidate:
    """Candidate with admission metadata required by the approval capital path."""
    from uuid import uuid4

    from core.enums import InstrumentThesis, SetupType, TradeAction
    from core.schemas import AdmissionSnapshot

    risk = max(entry - stop, entry * 0.005)
    min_target = entry + risk * 2.6
    use_target = max(target, min_target)
    rr = (use_target - entry) / risk if risk > 0 else 2.6
    snap = AdmissionSnapshot(
        price_at_creation=entry,
        atr_at_creation=risk,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=80,
        entry_quality_at_creation=75,
        stop_at_creation=stop,
        target_at_creation=use_target,
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
        target=Decimal(str(use_target)),
        risk_reward=round(rr, 2),
        reasons=["test admission-ready"],
        strategy_version=strategy_version,
        pipeline_run_id=uuid4(),
        thesis=InstrumentThesis.BULLISH,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=80,
        entry_quality=75,
        market_label=market_label,
        admission_version="admission@1.1.0",
        admission_snapshot=snap.model_dump(mode="json"),
        entry_zone_low=Decimal(str(round(entry * 0.995, 4))),
        entry_zone_high=Decimal(str(round(entry * 1.005, 4))),
    )


def ensure_admission_ready(candidate: TradeCandidate) -> TradeCandidate:
    """Attach admission metadata to a bare test candidate without changing geometry intent."""
    if candidate.admission_version and candidate.admission_snapshot:
        # Still lift target to clear effective-RR floor when needed.
        risk = float(candidate.entry) - float(candidate.stop)
        if risk > 0:
            min_target = float(candidate.entry) + risk * 2.6
            if float(candidate.target) < min_target:
                return candidate.model_copy(
                    update={
                        "target": Decimal(str(round(min_target, 4))),
                        "risk_reward": round((min_target - float(candidate.entry)) / risk, 2),
                    }
                )
        return candidate
    return admission_ready_candidate(
        symbol=candidate.symbol,
        entry=float(candidate.entry),
        stop=float(candidate.stop),
        target=float(candidate.target),
        market_label=candidate.market_label or "neutral",
        strategy_version=candidate.strategy_version,
    ).model_copy(
        update={
            "pipeline_run_id": candidate.pipeline_run_id,
            "reasons": candidate.reasons,
            "confidence": candidate.confidence,
        }
    )
