"""Calibrated ORB (2.4/2.5): confirmation entry, structural stop, intraday tape filter."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from core.clock import ET
from core.enums import Timeframe, TradeAction
from core.schemas import Bar, TradeCandidate
from risk.position_sizing import size_long_shares
from risk.risk_engine import RiskContext
from strategy.orb import (
    CALIBRATED_PARAMETERS,
    INTRADAY_VERSION,
    PDH_VERSION,
    VERSION,
    _previous_sessions,
    evaluate_trigger,
    form_plan,
    parameters_for,
)
from strategy.orb.market_filter import (
    BELOW_VWAP,
    DATA_MISSING,
    DOWN_DAY,
    SYMBOL_BELOW_VWAP,
    WEAK_RELATIVE_STRENGTH,
    evaluate_intraday_market,
)
from strategy.orb.retest import rebuild
from strategy.orb.sizing import with_exit_slippage
from tests.unit.test_orb_policy import NOW, evidence, quote

# Five-minute pattern after the 09:30 range (high 101.00, daily ATR 2.00):
# breakout, extension to 103.50, return to the level, then confirmation.
PATTERN = [
    ("101.00", "101.60", "100.95", "101.50"),
    ("101.50", "102.40", "101.45", "102.30"),
    ("102.30", "103.50", "102.20", "103.10"),
    ("103.10", "103.15", "101.90", "102.00"),
    ("102.00", "102.05", "101.05", "101.20"),
    ("101.20", "101.70", "101.15", "101.65"),
]
CONFIRMED = NOW + timedelta(minutes=5)  # end of the sixth bar, 10:05 ET


def calibrated_evidence():
    daily, opening = evidence()
    # Previous-day high below the entry: the observed peak is the target.
    daily = [
        b.model_copy(update={"high": D("100.8"), "low": D("98.8"), "close": D("100.5")})
        for b in daily
    ]
    opening[-1] = opening[-1].model_copy(
        update={"open": D("100.5"), "high": D(101), "low": D("100.4"), "close": D("100.9")}
    )
    return daily, opening


def bars(pattern=PATTERN, start=None, symbol="AAPL"):
    start = start or datetime.combine(NOW.astimezone(ET).date(), time(9, 35), ET)
    return [
        Bar(
            symbol=symbol,
            timeframe=Timeframe.M5,
            ts=start + timedelta(minutes=5 * i),
            open=D(o),
            high=D(h),
            low=D(low),
            close=D(c),
            volume=D(100000),
            source="alpaca",
        )
        for i, (o, h, low, c) in enumerate(pattern)
    ]


def base_plan(version=VERSION):
    daily, opening = calibrated_evidence()
    plan = form_plan("AAPL", daily, opening, now=NOW, feed="sip", version=version).plan
    assert plan is not None
    return plan


def ready_plan(pattern=PATTERN):
    return rebuild(base_plan(), bars(pattern), now=CONFIRMED)


def test_confirmation_geometry_uses_structural_buffer_and_entry_cap():
    decision = ready_plan()
    plan = decision.plan
    retest = plan.evidence["retest"]
    assert decision.reasons == ["ORB_RETEST_CONFIRMED"]
    assert plan.version == VERSION
    # Stop: retest low 101.05 minus 0.10 x daily ATR 2.00.
    assert plan.stop == D("100.85")
    assert D(retest["stop_buffer"]) == D("0.20")
    # Cap: confirmation close 101.65 plus 0.10 R (R = 0.80 at the close).
    assert plan.max_entry == D("101.73")
    assert retest["confirmation_close"] == "101.65"
    assert retest["target"] == "103.50"
    assert retest["previous_day_high_state"] == "below_entry"
    # One bar of signal life, not two.
    assert datetime.fromisoformat(retest["valid_until"]) == CONFIRMED + timedelta(minutes=5)
    assert plan.evidence["parameters"]["entry_policy_revision"] == "paper-confirmation-1"


def test_buys_the_confirmation_instead_of_waiting_for_another_return():
    plan = ready_plan().plan
    allowed = evaluate_trigger(plan, quote("101.64", "101.66", CONFIRMED), now=CONFIRMED)
    assert allowed.state == "BUY_ALLOWED"
    # Above the cap is a chase, not an entry.
    chased = evaluate_trigger(plan, quote("101.73", "101.75", CONFIRMED), now=CONFIRMED)
    assert chased.reasons == ["ORB_WAITING_PULLBACK"]


def test_signal_lives_one_bar():
    plan = ready_plan().plan
    later = CONFIRMED + timedelta(minutes=5)
    decision = evaluate_trigger(plan, quote("101.64", "101.66", later), now=later)
    assert decision.reasons == ["ORB_RETEST_EXPIRED"]


def test_spread_wider_than_half_the_stop_buffer_waits():
    plan = ready_plan().plan
    wide = evaluate_trigger(plan, quote("101.50", "101.61", CONFIRMED), now=CONFIRMED)
    assert (wide.state, wide.reasons) == ("WAIT", ["ORB_SPREAD_TOO_WIDE"])
    narrow = evaluate_trigger(plan, quote("101.55", "101.65", CONFIRMED), now=CONFIRMED)
    assert narrow.state == "BUY_ALLOWED"


def test_stop_inside_intraday_noise_is_refused():
    # Same structure on bars twice as wide: 0.5 x M5 ATR exceeds the stop distance.
    wild = [
        ("101.00", "102.60", "100.40", "101.50"),
        ("101.50", "104.40", "101.45", "104.30"),
        ("104.30", "106.50", "103.20", "105.10"),
        ("105.10", "105.15", "101.90", "102.00"),
        ("102.00", "102.05", "101.05", "101.20"),
        ("101.20", "101.70", "101.15", "101.65"),
    ]
    decision = ready_plan(wild)
    assert not decision.plan.evidence.get("retest")
    assert decision.reasons == ["ORB_STOP_IN_NOISE"]


def test_stop_wider_than_daily_atr_is_refused():
    deep = [*PATTERN[:4], ("102.00", "102.05", "98.60", "101.20"), PATTERN[5]]
    decision = ready_plan(deep)
    assert not decision.plan.evidence.get("retest")
    assert decision.reasons == ["ORB_STOP_TOO_WIDE"]


def test_frozen_versions_keep_their_parameters():
    assert parameters_for(PDH_VERSION)["signal_ttl_minutes"] == 10
    assert parameters_for(PDH_VERSION)["retest_stop_buffer_atr"] == "0.02"
    assert parameters_for(VERSION) is CALIBRATED_PARAMETERS
    assert parameters_for(INTRADAY_VERSION) is CALIBRATED_PARAMETERS
    legacy = base_plan(PDH_VERSION)
    assert "entry_cap_r" not in legacy.evidence["parameters"]


def test_sizing_charges_exit_slippage_to_the_risk_budget():
    plain = size_long_shares(
        equity=D(100000),
        entry=D("101.73"),
        stop=D("100.85"),
        risk_pct=0.5,
        max_position_pct=100.0,
        cash=D(100000),
    )
    slipped = size_long_shares(
        equity=D(100000),
        entry=D("101.73"),
        stop=D("100.85"),
        risk_pct=0.5,
        max_position_pct=100.0,
        cash=D(100000),
        slippage_per_share=D("0.12"),
    )
    assert slipped[0] < plain[0]
    # The budget still holds once the slippage is paid.
    assert slipped[1] <= D(500)
    assert size_long_shares(
        equity=D(100000),
        entry=D(101),
        stop=D(100),
        risk_pct=1.0,
        max_position_pct=5.0,
        cash=D(100000),
        slippage_per_share=D(-1),
    ) == (D(0), D(0))


def candidate(version):
    plan = ready_plan().plan
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0,
        entry=plan.max_entry,
        stop=plan.stop,
        exit_policy="session_close",
        exit_at=plan.exit_at,
        orb_plan=plan.model_dump(mode="json"),
        strategy_version=version,
        exec_timeframe=Timeframe.M5,
        reasons=["ORB_RETEST_CONFIRMED"],
    )


def test_exit_slippage_applies_to_calibrated_entries_only():
    ctx = RiskContext()
    live = quote("101.60", "101.64", CONFIRMED)
    assert with_exit_slippage(ctx, candidate(VERSION), live).exit_slippage_per_share == D("0.04")
    assert with_exit_slippage(ctx, candidate(VERSION), None) is ctx
    legacy = candidate(VERSION).model_copy(update={"strategy_version": PDH_VERSION})
    assert with_exit_slippage(ctx, legacy, live) is ctx
    assert replace(ctx, exit_slippage_per_share=D(0)) == ctx


# --- intraday market filter ------------------------------------------------

OPEN = datetime.combine(NOW.astimezone(ET).date(), time(9, 30), ET)
AT = OPEN + timedelta(minutes=30)  # six completed bars


def series(closes, symbol="SPY", start=OPEN):
    rows, previous = [], D(closes[0]) - D("0.10")
    for i, c in enumerate(closes):
        close = D(c)
        rows.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.M5,
                ts=start + timedelta(minutes=5 * i),
                open=previous,
                high=max(previous, close) + D("0.05"),
                low=min(previous, close) - D("0.05"),
                close=close,
                volume=D(1000),
                source="alpaca",
            )
        )
        previous = close
    return rows


RISING = ["500.0", "500.2", "500.4", "500.6", "500.8", "501.0"]
LEADER = ["100.0", "100.4", "100.8", "101.2", "101.6", "102.0"]


def tape(spy=RISING, stock=LEADER, prior_close=D(500), now=AT):
    return evaluate_intraday_market(
        benchmark_bars=series(spy),
        benchmark_prior_close=prior_close,
        symbol_bars=series(stock, symbol="AAPL"),
        now=now,
        parameters=CALIBRATED_PARAMETERS,
    )


def test_market_filter_passes_a_leader_in_a_rising_tape():
    result = tape()
    assert result.passed and result.data_ok and result.reasons == ()
    assert result.measured["benchmark_vwap"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"spy": ["501.0", "500.8", "500.6", "500.4", "500.2", "500.0"]}, BELOW_VWAP),
        ({"prior_close": D(510)}, DOWN_DAY),
        (
            {"stock": ["100.0", "100.02", "100.04", "100.06", "100.08", "100.1"]},
            WEAK_RELATIVE_STRENGTH,
        ),
        ({"stock": ["100.0", "101.5", "101.4", "100.8", "100.5", "100.3"]}, SYMBOL_BELOW_VWAP),
    ],
)
def test_market_filter_refusals(kwargs, reason):
    result = tape(**kwargs)
    assert result.data_ok and not result.passed
    assert reason in result.reasons


def test_one_close_back_above_vwap_is_not_enough():
    # Five falling bars, then one bounce above VWAP: hysteresis needs two closes.
    result = tape(spy=["501.0", "500.2", "499.8", "499.6", "499.4", "500.9"])
    assert BELOW_VWAP in result.reasons


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prior_close": None},
        {"spy": RISING[:3]},  # stopped three bars ago: stale tape
        {"now": AT + timedelta(minutes=15)},  # same: bars end long before now
    ],
)
def test_market_filter_fails_closed_on_missing_or_stale_data(kwargs):
    result = tape(**kwargs)
    assert (result.passed, result.data_ok, result.reasons) == (False, False, (DATA_MISSING,))


def test_market_filter_refuses_a_gap_in_the_tape():
    spy = series(RISING)
    del spy[2]
    result = evaluate_intraday_market(
        benchmark_bars=spy,
        benchmark_prior_close=D(500),
        symbol_bars=series(LEADER, symbol="AAPL"),
        now=AT,
        parameters=CALIBRATED_PARAMETERS,
    )
    assert result.reasons == (DATA_MISSING,)


@pytest.mark.asyncio
async def test_unreadable_tape_is_a_refusal():
    from strategy.orb.market_filter import assess_intraday_market

    class Broken:
        async def get_bars(self, *args):
            raise TimeoutError

    result = await assess_intraday_market(
        "AAPL", Broken(), now=AT.astimezone(UTC), parameters=CALIBRATED_PARAMETERS
    )
    assert result.reasons == (DATA_MISSING,)


# --- full approval path ----------------------------------------------------


def calibrated_session(now):
    """A 2.4 plan confirmed on the bar that closes at `now`, saved as selected."""
    from database.models.orb import OrbSessionRow
    from database.session import session_factory

    daily, opening = evidence(now)
    daily = [
        b.model_copy(update={"high": D("100.8"), "low": D("98.8"), "close": D("100.5")})
        for b in daily
    ]
    opening[-1] = opening[-1].model_copy(
        update={"open": D("100.5"), "high": D(101), "low": D("100.4"), "close": D("100.9")}
    )
    base = form_plan("AAPL", daily, opening, now=now, feed="sip").plan
    flat = ("100.60", "100.90", "100.50", "100.70")
    count = int((now - base.range_end).total_seconds() // 300)
    rows = bars([flat] * (count - len(PATTERN)) + PATTERN, start=base.range_end)
    plan = rebuild(base, rows, now=now).plan
    assert plan.evidence.get("retest")
    with session_factory()() as db:
        db.add(
            OrbSessionRow(
                session=plan.session,
                payload={
                    "session": plan.session,
                    "version": plan.version,
                    "plans": {plan.symbol: plan.model_dump(mode="json")},
                    "states": {},
                },
            )
        )
        db.commit()
    return plan, [Bar.model_validate(opening[-1]), *rows]


def tape_market(plan, day_bars, spy_closes):
    from tests.support import LiquidMarketData

    session_open = plan.range_start
    spy = series(spy_closes, start=session_open)
    previous = Bar(
        symbol="SPY",
        timeframe=Timeframe.D1,
        ts=datetime.combine(
            _previous_sessions(plan.range_end, 1)[0],
            time(0),
            ET,
        ),
        open=D(499),
        high=D(501),
        low=D(498),
        close=D(500),
        volume=D(10**7),
        source="alpaca",
    )

    class TapeMarket(LiquidMarketData):
        async def get_bars(self, symbol, timeframe, start, end):
            if timeframe == Timeframe.M5 and symbol == "SPY":
                return [b for b in spy if start <= b.ts <= end]
            if timeframe == Timeframe.D1 and symbol == "SPY":
                return [previous]
            if timeframe == Timeframe.M5 and symbol == plan.symbol:
                if start == plan.range_start and end < plan.range_end:
                    return [day_bars[0]]
                return [b for b in day_bars if start <= b.ts <= end]
            return await super().get_bars(symbol, timeframe, start, end)

    return TapeMarket(price=101.10)


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
@pytest.mark.parametrize("tape_ok", [True, False])
async def test_calibrated_entry_executes_with_tight_limit_or_refuses_on_tape(tape_ok):
    from broker.paper.mock import MockPaperBroker
    from core.audit import InMemoryAudit
    from core.enums import EntryDecision, OpportunityStatus, SetupType, TradingMode, UserDecision
    from risk.risk_engine import RiskEngine
    from tests.conftest import RTH_INSTANT
    from tests.support import CLEARED_EARNINGS
    from trading.execution import ExecutionService
    from trading.exits import MemoryExitStore
    from trading.opportunities import MemoryOpportunityStore

    plan, day_bars = calibrated_session(RTH_INSTANT)
    count = len(day_bars)
    rising = [str(D(500) + D("0.1") * i) for i in range(count)]
    falling = [str(D(500) - D("0.1") * i) for i in range(count)]
    market = tape_market(plan, day_bars, rising if tape_ok else falling)
    candidate = TradeCandidate(
        symbol=plan.symbol,
        action=TradeAction.BUY,
        confidence=0,
        entry=plan.max_entry,
        stop=plan.stop,
        exit_policy="session_close",
        exit_at=plan.exit_at,
        orb_plan=plan.model_dump(mode="json"),
        reasons=["ORB_RETEST_CONFIRMED"],
        strategy_version=plan.version,
        exec_timeframe=Timeframe.M5,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        entry_decision=EntryDecision.BUY_NOW,
        admission_version=plan.version,
        policy_version=plan.version,
    )
    broker = MockPaperBroker()
    risk = RiskEngine().evaluate(candidate, await broker.get_portfolio(), context=CLEARED_EARNINGS)
    store = MemoryOpportunityStore()
    opp = store.create(candidate, risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        broker=broker,
        market_data=market,
        store=store,
        exit_store=MemoryExitStore(),
        audit=InMemoryAudit(),
    )
    if not tape_ok:
        with pytest.raises(RuntimeError, match="MARKET_BELOW_VWAP"):
            await service.decide(
                opp.id,
                UserDecision.APPROVE,
                request_id=uuid4(),
                expected_decision_version=opp.decision_version,
            )
        assert broker.orders == []
        return
    result = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    assert result.status == OpportunityStatus.EXECUTED
    (buy,) = [o for o in broker.orders if o.side.value == "buy"]
    quote_now = await market.get_quote(plan.symbol)
    spread = quote_now.ask - quote_now.bid
    # Marketable by two spreads, far below the 101.73 cap.
    assert buy.limit_price == min(
        plan.max_entry, (quote_now.ask + 2 * spread).quantize(D("0.01"), rounding="ROUND_CEILING")
    )
    assert buy.limit_price < plan.max_entry
    assert any(o.order_type.value == "stop" and o.stop_price == plan.stop for o in broker.orders)
