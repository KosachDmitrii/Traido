"""Observable configured Alpaca feed access, checked even while the exchange is closed."""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

DATA_ACCESS: dict[str, Any] = {"status": "unchecked"}


def data_error_reason(exc: Exception, *, feed: str = "sip") -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 403:
            if feed != "sip":
                return "ORB_DATA_ACCESS_DENIED"
            if "subscription does not permit" in exc.response.text.lower():
                return "ORB_SIP_SUBSCRIPTION_REQUIRED"
            return "ORB_SIP_ACCESS_DENIED"
        if exc.response.status_code == 401:
            return "ORB_DATA_CREDENTIALS_REJECTED"
        if exc.response.status_code == 429:
            return "ORB_DATA_RATE_LIMITED"
    return "ORB_SERVICE_UNAVAILABLE"


async def check_access(market_data: Any) -> dict[str, Any]:
    reason = None
    feed = getattr(market_data, "_feed", None)
    if feed not in {"iex", "sip"}:
        reason = "ORB_UNSUPPORTED_FEED"
    else:
        try:
            # A stale/empty overnight quote is not an entitlement failure.
            # Freshness and geometry are checked separately for every entry.
            await market_data.get_quote("SPY")
        except Exception as exc:  # noqa: BLE001 — any probe failure reports data unavailable
            reason = data_error_reason(exc, feed=feed)
    if DATA_ACCESS.get("reason") != reason or DATA_ACCESS.get("status") == "unchecked":
        logging.getLogger(__name__).warning(
            "ORB data access (%s): %s", feed, reason or "accessible"
        )
    DATA_ACCESS.update(
        status="blocked" if reason else "accessible",
        feed=feed,
        reason=reason,
        checked_at=datetime.now(UTC).isoformat(),
    )
    from core.desk_bus import DESK_BUS

    DESK_BUS.bump_desk(kind="market_data")
    return dict(DATA_ACCESS)
