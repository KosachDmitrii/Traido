"""Strategy-supplied sizing inputs for the deterministic risk engine."""

from dataclasses import replace
from decimal import Decimal

from core.schemas import Quote, TradeCandidate
from risk.risk_engine import RiskContext
from strategy.orb import CALIBRATED_VERSIONS, parameters_for


def with_exit_slippage(
    ctx: RiskContext, candidate: TradeCandidate, quote: Quote | None
) -> RiskContext:
    """Charge calibrated ORB entries live spreads per share of exit slippage.

    A stop is filled at the market below its price; sizing to the stop alone
    understates the loss. Without a usable quote the context is unchanged, and
    the entry gates have already refused a missing or crossed book.
    """
    version = candidate.strategy_version
    if version not in CALIBRATED_VERSIONS or quote is None or quote.ask < quote.bid:
        return ctx
    spreads = Decimal(parameters_for(version)["sizing_slippage_spreads"])
    return replace(ctx, exit_slippage_per_share=(quote.ask - quote.bid) * spreads)
