"""Stage 1 and Stage 2 — the cheap filters that make a large universe affordable.

Both stages exist to remove names before anything expensive touches them, and
both are therefore judged on two things: that they reject for reasons that are
true, and that the order they produce is a function of the data alone.

The data-quality tests matter more here than anywhere else in the scanner. A
thousand names a day will include a zero price, a crossed book, a high below its
low and a feed that stopped last week. Every one of those has a plausible
reading as a *good* number — free, tight, and quiet — so defaulting them to zero
and continuing would make bad data look like the most attractive candidate on
the desk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.scanner.prefilter import (
    MarketFilterPolicy,
    MarketFilterReason,
    apply_market_filter,
    evaluate_snapshot,
)
from agents.scanner.prerank import PrerankPolicy, PrerankReason, prerank, score_candidate
from core.enums import Timeframe
from core.schemas import Bar, Snapshot
from universe.models import AssetClass, Instrument

NOW = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)


def _instrument(symbol: str = "AAAA") -> Instrument:
    return Instrument(
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        exchange="NASDAQ",
        currency="USD",
    )


def _snapshot(symbol: str = "AAAA", **overrides: object) -> Snapshot:
    base: dict[str, object] = {
        "symbol": symbol,
        "price": Decimal(100),
        "bid": Decimal("99.99"),
        "ask": Decimal("100.01"),
        "day_volume": Decimal(1_000_000),
        "day_high": Decimal(101),
        "day_low": Decimal(99),
        "prev_close": Decimal("99.5"),
        "trade_ts": NOW,
        "quote_ts": NOW,
    }
    base.update(overrides)
    return Snapshot(**base)  # type: ignore[arg-type]


# ── Stage 1 ─────────────────────────────────────────────────────────────────


def test_a_liquid_name_passes() -> None:
    result = evaluate_snapshot(_instrument(), _snapshot(), MarketFilterPolicy(), now=NOW)
    assert result.passed
    assert result.measured["dollar_volume"] == pytest.approx(100_000_000)


def test_a_symbol_the_batch_did_not_answer_for_is_named_as_such() -> None:
    """Absent must not arrive as a snapshot full of `None` that reads as illiquid."""
    result = evaluate_snapshot(_instrument(), None, MarketFilterPolicy(), now=NOW)
    assert not result.passed
    assert result.reasons == (MarketFilterReason.NO_SNAPSHOT,)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"price": None}, MarketFilterReason.MISSING_PRICE),
        ({"price": Decimal(0)}, MarketFilterReason.INVALID_PRICE),
        ({"price": Decimal(-5)}, MarketFilterReason.INVALID_PRICE),
        ({"day_volume": None}, MarketFilterReason.MISSING_VOLUME),
        ({"price": Decimal(2)}, MarketFilterReason.PRICE_BELOW_MINIMUM),
        ({"day_volume": Decimal(100)}, MarketFilterReason.INSUFFICIENT_DOLLAR_VOLUME),
        ({"day_high": Decimal(90), "day_low": Decimal(99)}, MarketFilterReason.INVALID_BARS),
        ({"bid": Decimal(101), "ask": Decimal(99)}, MarketFilterReason.CROSSED_BOOK),
        ({"bid": Decimal(90), "ask": Decimal(110)}, MarketFilterReason.SPREAD_TOO_WIDE),
    ],
)
def test_bad_data_is_rejected_rather_than_defaulted(overrides: dict, reason: str) -> None:
    result = evaluate_snapshot(_instrument(), _snapshot(**overrides), MarketFilterPolicy(), now=NOW)
    assert not result.passed
    assert reason in result.reasons


def test_a_feed_that_stopped_is_not_a_quiet_symbol() -> None:
    """A stale timestamp returns a pass unless someone checks it."""
    stale = _snapshot(trade_ts=NOW - timedelta(hours=6))
    result = evaluate_snapshot(_instrument(), stale, MarketFilterPolicy(), now=NOW)

    assert not result.passed
    assert MarketFilterReason.STALE_DATA in result.reasons


def test_a_crossed_book_is_not_a_tight_spread() -> None:
    """The bad reading is the attractive one, which is why this is explicit."""
    crossed = _snapshot(bid=Decimal(101), ask=Decimal(99))
    assert crossed.spread_bps is None


def test_a_missing_quote_does_not_reject_by_default() -> None:
    """Not a weakening of the liquidity gate, which is untouched and fails closed.

    That gate reads a *live* quote at the click. This reads a batched snapshot
    whose quote side is thinner, and rejecting here would discard candidates the
    real gate would have priced correctly.
    """
    result = evaluate_snapshot(
        _instrument(), _snapshot(bid=None, ask=None), MarketFilterPolicy(), now=NOW
    )
    assert result.passed


def test_a_missing_quote_can_be_made_fatal_by_policy() -> None:
    result = evaluate_snapshot(
        _instrument(),
        _snapshot(bid=None, ask=None),
        MarketFilterPolicy(require_quote=True),
        now=NOW,
    )
    assert not result.passed


def test_the_prefilter_cut_is_by_value_then_symbol_never_by_arrival() -> None:
    """A cut that depended on response order would make the desk unreproducible."""
    instruments = [_instrument(s) for s in ("CCCC", "AAAA", "BBBB")]
    snapshots = {
        "AAAA": _snapshot("AAAA", day_volume=Decimal(1_000_000)),
        "BBBB": _snapshot("BBBB", day_volume=Decimal(3_000_000)),
        "CCCC": _snapshot("CCCC", day_volume=Decimal(2_000_000)),
    }

    outcome = apply_market_filter(instruments, snapshots, limit=2, now=NOW)

    assert [c.symbol for c in outcome.passed] == ["BBBB", "CCCC"]


def test_a_name_cut_by_the_limit_says_so_rather_than_vanishing() -> None:
    instruments = [_instrument(s) for s in ("AAAA", "BBBB")]
    snapshots = {s: _snapshot(s) for s in ("AAAA", "BBBB")}

    outcome = apply_market_filter(instruments, snapshots, limit=1, now=NOW)

    assert len(outcome.passed) == 1
    assert len(outcome.rejected) == 1
    assert MarketFilterReason.OUTRANKED_BY_LIMIT in outcome.rejected[0].reasons


def test_nothing_is_lost_between_passed_and_rejected() -> None:
    instruments = [_instrument(f"AA{chr(65 + i)}{chr(65 + i)}") for i in range(10)]
    snapshots = {i.key: _snapshot(i.key) for i in instruments}

    outcome = apply_market_filter(instruments, snapshots, limit=4, now=NOW)

    assert len(outcome.passed) + len(outcome.rejected) == 10


# ── Stage 2 ─────────────────────────────────────────────────────────────────


def _bars(symbol: str, count: int = 120, *, drift: float = 0.002, end: datetime = NOW) -> list[Bar]:
    out: list[Bar] = []
    for i in range(count):
        close = 100.0 * (1.0 + drift * i)
        out.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=end - timedelta(days=count - i),
                open=Decimal(str(round(close * 0.995, 4))),
                high=Decimal(str(round(close * 1.01, 4))),
                low=Decimal(str(round(close * 0.99, 4))),
                close=Decimal(str(round(close, 4))),
                volume=Decimal(1_000_000),
                source="test",
            )
        )
    return out


def test_a_trending_name_scores_above_a_flat_one() -> None:
    strong = score_candidate(
        _instrument("AAAA"), _bars("AAAA", drift=0.004), PrerankPolicy(), now=NOW
    )
    flat = score_candidate(_instrument("BBBB"), _bars("BBBB", drift=0.0), PrerankPolicy(), now=NOW)

    assert strong.passed and flat.passed
    assert strong.quant_score > flat.quant_score


def test_a_name_without_enough_history_is_not_scored_optimistically() -> None:
    """A 50-day trend measured on 20 bars is a 20-day trend wearing a label."""
    result = score_candidate(_instrument(), _bars("AAAA", count=20), PrerankPolicy(), now=NOW)

    assert not result.passed
    assert PrerankReason.INSUFFICIENT_HISTORY in result.reasons


def test_a_dead_series_is_refused_rather_than_ranked_on_an_old_trend() -> None:
    old = _bars("AAAA", end=NOW - timedelta(days=30))
    result = score_candidate(_instrument(), old, PrerankPolicy(), now=NOW)

    assert not result.passed
    assert PrerankReason.STALE_BARS in result.reasons


def test_no_bars_at_all_is_its_own_reason() -> None:
    result = score_candidate(_instrument(), [], PrerankPolicy(), now=NOW)
    assert PrerankReason.NO_BARS in result.reasons


def test_the_shortlist_is_the_top_k_and_the_rest_are_marked_outranked() -> None:
    instruments = [_instrument(f"AA{chr(65 + i)}A") for i in range(10)]
    bars = {inst.key: _bars(inst.key, drift=0.0005 * (n + 1)) for n, inst in enumerate(instruments)}

    outcome = prerank(instruments, bars, top_k=3, now=NOW)

    assert len(outcome.shortlist) == 3
    assert len(outcome.shortlist) + len(outcome.outranked) + len(outcome.rejected) == 10
    assert all(PrerankReason.OUTRANKED in c.reasons for c in outcome.outranked)


def test_the_shortlist_order_is_stable_under_input_order() -> None:
    """Two runs over the same data in different order must agree exactly.

    The score is rounded before comparison so floating-point noise in the
    fifteenth decimal place cannot decide a place — otherwise "identical every
    run" would be luck rather than a guarantee.
    """
    instruments = [_instrument(f"AA{chr(65 + i)}A") for i in range(12)]
    bars = {i.key: _bars(i.key, drift=0.002) for i in instruments}

    forward = prerank(instruments, bars, top_k=5, now=NOW)
    backward = prerank(list(reversed(instruments)), bars, top_k=5, now=NOW)

    assert [c.symbol for c in forward.shortlist] == [c.symbol for c in backward.shortlist]


def test_a_candidate_keeps_the_event_time_of_the_data_it_was_scored_on() -> None:
    """Provenance: when the data happened, not when we computed on it."""
    bars = _bars("AAAA")
    result = score_candidate(_instrument(), bars, PrerankPolicy(), now=NOW)

    assert result.data_ts == bars[-1].ts


def test_a_thin_average_is_refused_even_after_one_busy_session() -> None:
    """Stage 1 rejects a liquid name having a dead day; this rejects the reverse."""
    thin = _bars("AAAA")
    thin = [b.model_copy(update={"volume": Decimal(100)}) for b in thin]

    result = score_candidate(_instrument(), thin, PrerankPolicy(), now=NOW)

    assert not result.passed
    assert "INSUFFICIENT_AVG_DOLLAR_VOLUME" in result.reasons
