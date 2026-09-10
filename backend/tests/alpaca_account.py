import httpx

from broker.alpaca import AlpacaPaperBroker


def account_broker(summary):
    def handle(request):
        if request.url.path == "/v2/account":
            return httpx.Response(200, json=summary)
        return httpx.Response(200, json=[])

    broker = AlpacaPaperBroker(
        "risk-key",
        "risk-secret",
        "https://paper-api.alpaca.markets",
        transport=httpx.MockTransport(handle),
    )
    broker._cache_clear()
    broker.summary = summary
    return broker
