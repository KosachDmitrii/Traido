"""Spread ceilings calibrated to the consolidated Alpaca SIP/NBBO feed."""

from __future__ import annotations


def max_spread_bps_for_feed(base_sip_bps: float, feed: str) -> float:
    """Return the SIP-calibrated spread ceiling and reject every other feed."""
    if feed.strip().lower() != "sip":
        raise ValueError("ALPACA_SIP_FEED_REQUIRED")
    return base_sip_bps
