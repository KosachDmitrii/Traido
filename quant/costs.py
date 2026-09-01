"""
Execution cost model — commissions, regulatory fees, spread, and slippage.

Backtest results without costs are marketing, not evidence. Every simulated
fill in Traido goes through this module so reported edge is net of what a real
broker would actually charge and where the order would actually fill.

Defaults model a retail US-equity account on a zero-commission broker
(Alpaca paper mirrors Alpaca live pricing):

- Commission:   $0 per share on US equities.
- SEC fee:      sells only, $27.80 per $1,000,000 of principal (2024 rate).
- FINRA TAF:    sells only, $0.000166/share, capped at $8.30 per order.
- Half-spread:  paid on entry and exit; liquid large caps sit near 2 bps.
- Slippage:     order-type dependent. Marketable orders pay a few bps;
                stop orders convert to market and pay materially more
                because they trigger into the move that hit them.

All prices in, all prices out are Decimal to stay exact on the money path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

_CENT = Decimal("0.01")
_PRICE_Q = Decimal("0.0001")
_BPS = Decimal(10000)


class FillKind(StrEnum):
    """How a simulated order reaches the tape — drives the slippage charged."""

    MARKET = "market"
    """Marketable order at a known reference price (signal exits, entries)."""

    LIMIT = "limit"
    """Resting limit that the market came to. No adverse slippage, spread only."""

    STOP = "stop"
    """Stop triggered into a move. Worst realistic fill of the three."""


@dataclass(frozen=True)
class CostModel:
    """Deterministic per-fill cost model. No randomness — backtests must be reproducible."""

    commission_per_share: Decimal = Decimal(0)
    commission_pct: Decimal = Decimal(0)
    commission_min: Decimal = Decimal(0)

    sec_fee_rate: Decimal = Decimal("0.0000278")
    """SEC Section 31 fee on sale proceeds. Sells only."""

    finra_taf_per_share: Decimal = Decimal("0.000166")
    finra_taf_max: Decimal = Decimal("8.30")
    """FINRA Trading Activity Fee. Sells only."""

    half_spread_bps: float = 2.0
    """Half the quoted bid-ask, paid on both sides of a round trip."""

    slippage_market_bps: float = 2.0
    slippage_limit_bps: float = 0.0
    slippage_stop_bps: float = 8.0

    @classmethod
    def zero(cls) -> CostModel:
        """Frictionless model. Only for isolating strategy logic in unit tests."""
        return cls(
            sec_fee_rate=Decimal(0),
            finra_taf_per_share=Decimal(0),
            half_spread_bps=0.0,
            slippage_market_bps=0.0,
            slippage_limit_bps=0.0,
            slippage_stop_bps=0.0,
        )

    @classmethod
    def conservative(cls) -> CostModel:
        """Wider assumptions for mid-caps or thinner tape. Use when in doubt."""
        return cls(
            half_spread_bps=5.0,
            slippage_market_bps=5.0,
            slippage_limit_bps=1.0,
            slippage_stop_bps=20.0,
        )

    def _adverse_bps(self, kind: FillKind) -> Decimal:
        if kind is FillKind.LIMIT:
            slip = self.slippage_limit_bps
        elif kind is FillKind.STOP:
            slip = self.slippage_stop_bps
        else:
            slip = self.slippage_market_bps
        return (Decimal(str(self.half_spread_bps)) + Decimal(str(slip))) / _BPS

    def fill_price(self, reference: Decimal, *, side: str, kind: FillKind) -> Decimal:
        """
        Reference price adjusted against us.

        Buys fill above the reference, sells below it. There is no fill kind
        where the simulated trader gets price improvement.
        """
        adverse = self._adverse_bps(kind)
        if side == "buy":
            price = reference * (Decimal(1) + adverse)
        else:
            price = reference * (Decimal(1) - adverse)
        return price.quantize(_PRICE_Q, rounding=ROUND_HALF_UP)

    def fees(self, qty: Decimal, price: Decimal, *, side: str) -> Decimal:
        """Commission plus regulatory fees for one fill."""
        if qty <= 0 or price <= 0:
            return Decimal(0)

        notional = qty * price
        commission = qty * self.commission_per_share + notional * self.commission_pct
        if self.commission_min > 0:
            commission = max(commission, self.commission_min)

        regulatory = Decimal(0)
        if side == "sell":
            regulatory += notional * self.sec_fee_rate
            taf = qty * self.finra_taf_per_share
            regulatory += min(taf, self.finra_taf_max)

        return (commission + regulatory).quantize(_CENT, rounding=ROUND_HALF_UP)

    def round_trip_cost_bps(self, price: Decimal, qty: Decimal) -> float:
        """
        Total friction for a round trip, in basis points of entry notional.

        Used as a tradability filter: a setup whose expected move is not
        several multiples of this number is noise, not edge.
        """
        if price <= 0 or qty <= 0:
            return 0.0
        buy_fill = self.fill_price(price, side="buy", kind=FillKind.MARKET)
        sell_fill = self.fill_price(price, side="sell", kind=FillKind.MARKET)
        spread_cost = (buy_fill - sell_fill) * qty
        fee_cost = self.fees(qty, buy_fill, side="buy") + self.fees(qty, sell_fill, side="sell")
        notional = price * qty
        return float((spread_cost + fee_cost) / notional * _BPS)


DEFAULT_COST_MODEL = CostModel()
