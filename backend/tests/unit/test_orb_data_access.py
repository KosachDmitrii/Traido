import httpx
import pytest

from strategy.orb.data_access import DATA_ACCESS, check_access


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason",
    [
        (403, "ORB_SIP_ACCESS_DENIED"),
        (401, "ORB_DATA_CREDENTIALS_REJECTED"),
        (429, "ORB_DATA_RATE_LIMITED"),
    ],
)
async def test_access_failure_is_named_and_successful_retry_clears_it(status, reason):
    class Feed:
        _feed = "sip"
        failed = True

        async def get_quote(self, symbol):
            assert symbol == "SPY"
            if self.failed:
                response = httpx.Response(
                    status, request=httpx.Request("GET", "https://data.alpaca.markets/test")
                )
                response.raise_for_status()

    feed = Feed()
    try:
        result = await check_access(feed)
        assert result["status"] == "blocked" and result["reason"] == reason
        feed.failed = False
        result = await check_access(feed)
        assert result["status"] == "accessible" and result["reason"] is None
    finally:
        DATA_ACCESS.clear()
        DATA_ACCESS["status"] = "unchecked"


def test_subscription_denial_is_distinct_from_bad_credentials():
    from strategy.orb.data_access import data_error_reason

    response = httpx.Response(
        403,
        json={"message": "subscription does not permit querying recent SIP data"},
        request=httpx.Request("GET", "https://data.alpaca.markets/test"),
    )
    error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
    assert data_error_reason(error) == "ORB_SIP_SUBSCRIPTION_REQUIRED"
