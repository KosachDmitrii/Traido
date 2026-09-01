"""Position Agent — watches open positions; emits ExitProposal only (no orders)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.activity import BOARD
from core.enums import Timeframe, TradeAction, UserDecision
from core.ports import BrokerPort, MarketDataPort
from core.schemas import ExitProposal
from quant.engine import compute_features
from trading.exits import (
    EXIT_AWAITING,
    EXIT_EXPIRED,
    EXITS,
    OPERATOR_CLOSE_REASON,
    ExitOpportunity,
)
from trading.gates import check_bar_freshness
from trading.ledger import LEDGER
from trading.pricing import round_trip_cost_pct

POSITION_VERSION = "position@0.2.0"

MIN_EXIT_PROFIT_PCT = round_trip_cost_pct()
"""How far up a position must be before "in profit" means anything.

The rules below that sell *because* a position is green were gated on
`pnl_pct > 0`, which is satisfied by a single tick — less than the spread paid
to open and pay again to close. A sell proposed there converts a rounding error
into a realised loss.
"""

DEFAULT_EXIT_TIMEFRAME = Timeframe.D1
"""Used only for positions opened before the entry timeframe was recorded."""

_LOOKBACK_DAYS: dict[Timeframe, int] = {
    Timeframe.D1: 400,
    Timeframe.H1: 120,
    Timeframe.M15: 30,
}
"""Enough bars for the slowest indicator on each series, matching the scanner.

The exit agent asked for 120 days regardless of timeframe, which on the daily
series is 83 bars — too few for EMA200, so it read `None` and any rule using it
would have been silently skipped.
"""

_MIN_BARS = 31
"""Thirty for the indicators, plus one so there is a previous bar to compare to."""


def _exec_timeframe(ledger_row: object | None) -> Timeframe:
    """The series this position's entry was drawn on."""
    payload = getattr(ledger_row, "payload", None) or {}
    raw = payload.get("exec_timeframe")
    if not raw:
        return DEFAULT_EXIT_TIMEFRAME
    try:
        return Timeframe(raw)
    except ValueError:
        return DEFAULT_EXIT_TIMEFRAME


def _num(value: object) -> float | None:
    """Indicator values are typed loosely; a bool is not a price."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _crossed_below(
    previous: Mapping[str, object],
    current: Mapping[str, object],
    fast_key: str,
    slow_key: str,
) -> bool:
    """True only on the bar where fast goes from at-or-above slow to below it.

    The rule this replaces compared the two levels on the newest bar alone and
    reported the result as "crossed below". A level is not an event: MO's daily
    SMA20 sat 3.3% under its EMA50 for weeks, so the condition was already true
    when the position was opened and fired again on every pass afterwards. An
    operator reading "crossed" is told something just happened, and nothing had.

    Unreadable on either bar is not a cross. A crossing we cannot see is not one
    we may claim, and the cost of missing it is a position that keeps its stop.
    """
    prev_fast, prev_slow = _num(previous.get(fast_key)), _num(previous.get(slow_key))
    now_fast, now_slow = _num(current.get(fast_key)), _num(current.get(slow_key))
    if prev_fast is None or prev_slow is None or now_fast is None or now_slow is None:
        return False
    return prev_fast >= prev_slow and now_fast < now_slow


def _rr_achieved(entry: Decimal, stop: Decimal | None, current: Decimal) -> float | None:
    if stop is None or entry <= stop:
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    return float((current - entry) / risk)


def _withdraw_unsupported(evaluated: set[str], proposed: set[str]) -> None:
    """Take down sell cards whose reason no longer holds.

    A proposal is a standing invitation to sell, and it outlived the condition
    that raised it: the desk went on offering to sell MO on a cross that had
    never happened long after the rule stopped believing it. Code and board
    disagreeing is worse than either being wrong alone, because the operator can
    only see one of them.

    The transition runs through `claim`, so a card already moving — approving,
    sold, held — is refused rather than yanked out from under the operator who
    just pressed it. A card the operator raised is skipped outright: a
    discretionary close has no rule behind it that could stop being true, and
    `close_position` writes one and claims it a moment later, so this pass could
    otherwise expire it inside that gap and answer the click with a state error.
    """
    for card in EXITS.list_open():
        symbol = card.proposal.symbol
        if symbol not in evaluated or symbol in proposed:
            continue
        if OPERATOR_CLOSE_REASON in card.proposal.reasons:
            continue
        if EXITS.claim(card.id, from_status=EXIT_AWAITING, to_status=EXIT_EXPIRED):
            BOARD.log(
                "position",
                "SELL proposal withdrawn · exit condition no longer holds",
                symbol=symbol,
            )


async def assess_exits(broker: BrokerPort, market_data: MarketDataPort) -> list[ExitOpportunity]:
    """Scan broker positions (+ Traido ledger metadata) for exit candidates."""
    positions = await broker.list_positions()
    out: list[ExitOpportunity] = []
    end = datetime.now(UTC)
    # Only symbols this pass actually reached a verdict on. A symbol skipped for
    # stale or missing bars is not a symbol we decided to stop selling, and
    # withdrawing its card on that basis would let a data outage clear the desk.
    evaluated: set[str] = set()

    if not positions:
        BOARD.set_agent("position", status="idle", detail="No open positions")
        return EXITS.list_open()

    BOARD.set_agent(
        "position",
        status="working",
        detail=f"Reviewing {len(positions)} positions",
    )
    BOARD.log("position", f"Monitoring {len(positions)} open position(s)")

    for pos in positions:
        try:
            BOARD.set_agent(
                "position",
                status="working",
                detail=f"Checking {pos.symbol}",
                symbol=pos.symbol,
            )
            ledger = LEDGER.find_open_by_symbol(pos.symbol)
            stop = pos.stop_price or (ledger.stop_price if ledger else None)
            target = pos.target_price or (ledger.target_price if ledger else None)
            if stop is not None:
                stop = Decimal(str(stop))
            if target is not None:
                target = Decimal(str(target))

            timeframe = _exec_timeframe(ledger)
            start = end - timedelta(days=_LOOKBACK_DAYS.get(timeframe, 400))
            bars = await market_data.get_bars(pos.symbol, timeframe, start, end)
            if len(bars) < _MIN_BARS:
                continue

            # An exit judged on a series that stopped updating is the same
            # defect as an entry priced from one, and the position agent had no
            # age limit at all. Refusing to propose is safe here in a way that
            # refusing an entry is not: the protective stop is already resting
            # at the broker and is untouched by our silence.
            fresh = check_bar_freshness(pos.symbol, bars, now=end)
            if not fresh.passed:
                BOARD.log(
                    "position",
                    f"Stale {timeframe.value} bars — no exit signal "
                    f"(newest {fresh.measured.get('newest_bar')})",
                    symbol=pos.symbol,
                    level="warn",
                )
                continue

            snap = compute_features(pos.symbol, timeframe, bars)
            previous = compute_features(pos.symbol, timeframe, bars[:-1])
            current = Decimal(str(snap.indicators.get("close") or pos.avg_entry))
            pnl_pct = float((current - pos.avg_entry) / pos.avg_entry * 100)
            rsi_v = snap.indicators.get("rsi_14")
            reasons: list[str] = []
            recommend = UserDecision.HOLD
            confidence = 0.55

            # Target / stop geometry from Traido ledger
            if target is not None and current >= target:
                reasons.append("Target reached")
                recommend = UserDecision.SELL
                confidence = max(confidence, 0.85)
            if stop is not None and current <= stop:
                reasons.append("Stop level breached")
                recommend = UserDecision.SELL
                confidence = max(confidence, 0.9)

            rr = _rr_achieved(pos.avg_entry, stop, current)
            if rr is not None and rr >= 1.5 and pnl_pct > 0:
                reasons.append(f"R:R achieved {rr:.1f}× — take profit zone")
                recommend = UserDecision.SELL
                confidence = max(confidence, 0.78 if rr >= 2 else 0.7)

            rsi = _num(rsi_v)
            if rsi is not None and rsi >= 75 and pnl_pct > MIN_EXIT_PROFIT_PCT:
                reasons.append(f"RSI {rsi:.0f} overbought with profit")
                recommend = UserDecision.SELL
                confidence = max(confidence, 0.7)

            if (
                _crossed_below(previous.indicators, snap.indicators, "sma_20", "ema_50")
                and pnl_pct > MIN_EXIT_PROFIT_PCT
            ):
                reasons.append(
                    f"SMA20 crossed below EMA50 on {timeframe.value} (profit {pnl_pct:.1f}%)"
                )
                recommend = UserDecision.SELL
                confidence = max(confidence, 0.72)

            if pnl_pct <= -8:
                reasons.append(f"Drawdown {pnl_pct:.1f}% from entry")
                recommend = UserDecision.SELL
                confidence = max(confidence, 0.8)

            if ledger and ledger.opened_at:
                opened = ledger.opened_at
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=UTC)
                days = (end - opened).days
                if days >= 20 and pnl_pct < 1:
                    reasons.append(f"Stale position {days}d with flat P&L")
                    recommend = UserDecision.SELL
                    confidence = max(confidence, 0.65)

            evaluated.add(pos.symbol)
            if recommend != UserDecision.SELL:
                continue

            proposal = ExitProposal(
                position_id=ledger.id if ledger else pos.id,
                symbol=pos.symbol,
                action=TradeAction.SELL,
                entry=pos.avg_entry,
                current=current,
                pnl_pct=pnl_pct,
                reasons=reasons or ["Exit signal"],
                recommendation=UserDecision.SELL,
                confidence=min(0.95, confidence),
            )
            item = EXITS.upsert(proposal)
            out.append(item)
            BOARD.log(
                "position",
                f"SELL proposed · {', '.join(reasons)}",
                symbol=pos.symbol,
            )
        except Exception as exc:  # noqa: BLE001
            BOARD.log("position", f"Skip {pos.symbol}: {exc}", level="warn")
            continue

    _withdraw_unsupported(evaluated, {item.proposal.symbol for item in out})

    BOARD.set_agent(
        "position",
        status="done" if out else "idle",
        detail=f"{len(out)} exit proposal(s)" if out else "No exit signals",
        score=len(out) * 20 if out else 0,
    )
    return out
