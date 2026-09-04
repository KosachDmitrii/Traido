"""Spread ceilings calibrated to the Alpaca quote feed.

SIP/NBBO is the consolidated tape — thresholds are tuned to those quotes.
IEX is a single-exchange feed and often prints wider bid/ask than NBBO; the
same numeric ceiling would false-reject liquid names (see Alpaca forum/docs).
"""

from __future__ import annotations

# Weak production step: SIP 42 bps → IEX 70 bps (single-exchange books run wider).
_IEX_WEAK_SIP_RATIO = 70.0 / 42.0


def max_spread_bps_for_feed(base_sip_bps: float, feed: str) -> float:
    """Return the spread ceiling for ``feed`` given the SIP-calibrated base."""
    if feed.strip().lower() == "sip":
        return base_sip_bps
    return round(base_sip_bps * _IEX_WEAK_SIP_RATIO, 1)
