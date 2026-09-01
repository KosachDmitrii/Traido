"""Broker price / qty rounding for US equities (Alpaca tick rules)."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

ENTRY_BUFFER_BPS = 10.0
"""How far above the offer an entry limit is willing to reach, in basis points.

Ten is comfortably inside the 30bps the liquidity gate already refuses to trade
a spread wider than, so the buffer can never be the reason a name passes the
gate and then fills badly. It is a ceiling on what we pay, not a target: a
crossing order fills at the offer, and the buffer is only consumed when the book
moves between our read and the venue's.

It lives here rather than beside the entry path because it is also what a
*profit* costs to realise: whoever asks "is this position actually up?" is
asking about the same number, and two copies of it would drift apart.
"""


def round_trip_cost_pct(*, buffer_bps: float = ENTRY_BUFFER_BPS) -> float:
    """What a position must gain before it is worth anything, in percent.

    Crossing the spread is paid twice — once to open and once to close — so a
    position showing less than this is not in profit, it is in the cost of
    having been opened. Exit rules that read `pnl > 0` were treating the second
    crossing as free and proposing sells on a gain that closing would erase.
    """
    return 2 * buffer_bps / 100


def round_equity_price(price: Decimal | float | str) -> Decimal:
    """
    Alpaca: stocks >= $1 → $0.01 tick; < $1 → $0.0001.
    """
    px = Decimal(str(price))
    if px <= 0:
        return px
    quantum = Decimal("0.01") if px >= 1 else Decimal("0.0001")
    return px.quantize(quantum, rounding=ROUND_HALF_UP)


def round_equity_qty(qty: Decimal | float | str, *, max_decimals: int = 4) -> Decimal:
    """Fractional shares — keep a safe precision for paper.

    Used for quantities the broker reports back to us. Never floors a fill to a
    whole share: a fraction we discarded here is a fraction the venue holds and
    the protective stop would not cover.
    """
    q = Decimal(str(qty))
    quant = Decimal(1).scaleb(-max_decimals)  # 10^-max_decimals
    return q.quantize(quant, rounding=ROUND_DOWN)


def marketable_buy_limit(ask: Decimal, *, buffer_bps: float) -> Decimal:
    """A BUY limit priced to cross the current offer, and no further.

    A limit rather than a market order because the buffer is the whole point:
    it is wide enough to survive the ticks between reading the book and the
    venue reading our order, and narrow enough that a book which gaps away
    fills nothing instead of filling at any price. A market order has no such
    ceiling, which is the one thing an entry gate cannot put back.
    """
    capped = ask * (Decimal(1) + Decimal(str(buffer_bps)) / Decimal(10_000))
    return round_equity_price(capped)


def format_qty(qty: Decimal) -> str:
    """The quantity as a venue should read it: no trailing zeros, no exponent.

    `Decimal("50.0000")` and `Decimal("50")` are the same fifty shares, but only
    one of them looks whole on the wire. A venue that decides "is this order
    fractional?" by looking at the string sees two different orders, so the
    padding that survives a `quantize` is removed here rather than left for the
    vendor to interpret. `normalize` alone would send fifty as `5E+1`.
    """
    return format(qty.normalize(), "f")


def round_order_qty(qty: Decimal | float | str) -> Decimal:
    """Whole shares, for a quantity we originate.

    Sizing produces a fractional share count, and Alpaca accepts a fraction only
    with `time_in_force=day`. A protective stop must outlive the session, so it
    goes GTC — which means a fractional entry buys a position whose stop the
    venue then refuses, leaving emergency close as the only way out. Rounding
    the entry down to whole shares is what keeps the stop placeable; it lowers
    the position slightly, so it can only reduce risk.
    """
    return Decimal(str(qty)).quantize(Decimal(1), rounding=ROUND_DOWN)
