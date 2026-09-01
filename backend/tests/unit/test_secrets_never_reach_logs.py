"""A vendor error must not carry our API key into the log or the audit trail.

Finnhub authenticates by query parameter, so `httpx` puts the key in the request
URL, and `HTTPStatusError` puts the URL in its message. The supervisor stringifies
that exception into three places at once: the activity board the desk UI renders,
the agent status line, and a durable `ScanJobFailed` audit row.

The result was a live API key printed on screen and written to the database every
time Finnhub returned a 503 — which, on the free tier, is often. A key in a log is
a key that has to be rotated, and one nobody notices leaking is one that never is.

Two defences, because either alone is thin:
  - the key travels in a header, so it is not in the URL to leak;
  - anything on its way to a log or the audit is scrubbed regardless, since the
    next vendor will make the same choice Finnhub did.
"""

from __future__ import annotations

import httpx
import pytest

SECRET = "daa8ad9r01qvosocjro0daa8ad9r01qvosocjrog"
"""Shaped like the real thing, from the screenshot that started this."""


def _vendor_error() -> httpx.HTTPStatusError:
    """The exact exception Finnhub produces on a 503, key and all."""
    url = (
        "https://finnhub.io/api/v1/company-news"
        f"?symbol=MU&from=2026-08-24&to=2026-08-31&token={SECRET}"
    )
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"Server error '503 Service Unavailable' for url '{url}'",
        request=request,
        response=httpx.Response(503, request=request),
    )


class TestRedaction:
    def test_a_token_in_a_url_is_scrubbed(self) -> None:
        from core.redaction import redact_secrets

        cleaned = redact_secrets(str(_vendor_error()))

        assert SECRET not in cleaned
        assert "503" in cleaned, "the operator still needs to know what went wrong"
        assert "finnhub.io" in cleaned, "and which vendor it was"

    @pytest.mark.parametrize(
        "param", ["token", "api_key", "apikey", "apiKey", "key", "secret", "password"]
    )
    def test_every_common_credential_parameter_is_scrubbed(self, param: str) -> None:
        from core.redaction import redact_secrets

        assert SECRET not in redact_secrets(f"https://v.example/x?{param}={SECRET}&sym=MU")

    def test_a_configured_secret_is_scrubbed_wherever_it_appears(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not every leak arrives shaped like a query string.

        A vendor that echoes the key back in a JSON error body, or a traceback
        that renders a config object, defeats pattern matching. The value we
        hold is the thing to search for.
        """
        from core.config import get_settings

        monkeypatch.setenv("FINNHUB_API_KEY", SECRET)
        get_settings.cache_clear()
        try:
            from core.redaction import redact_secrets

            assert SECRET not in redact_secrets(f'{{"error": "bad key {SECRET}"}}')
        finally:
            get_settings.cache_clear()

    def test_ordinary_text_is_left_alone(self) -> None:
        """Over-eager redaction hides the diagnosis along with the secret."""
        from core.redaction import redact_secrets

        message = "Server error '503 Service Unavailable' for url 'https://finnhub.io/api/v1/quote'"
        assert redact_secrets(message) == message

    def test_a_short_or_empty_secret_does_not_scrub_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset key is the empty string, and `"" in anything` is true.

        Left unguarded this would replace every character of every message.
        """
        from core.config import get_settings

        monkeypatch.setenv("FINNHUB_API_KEY", "")
        get_settings.cache_clear()
        try:
            from core.redaction import redact_secrets

            assert redact_secrets("a plain message") == "a plain message"
        finally:
            get_settings.cache_clear()


class TestTheKeyIsNotInTheUrlToBeginWith:
    """Redaction is the net. Not putting the key in the URL is the fix."""

    @pytest.mark.asyncio
    async def test_news_sends_the_key_as_a_header(self) -> None:
        from agents.news.agent import assess_news

        seen: dict[str, httpx.Request] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        await assess_news("MU", SECRET, transport=transport)

        request = seen["request"]
        assert SECRET not in str(request.url), "the key must not be in the URL"
        assert request.headers.get("X-Finnhub-Token") == SECRET

    @pytest.mark.asyncio
    async def test_the_earnings_calendar_sends_the_key_as_a_header(self) -> None:
        from market_data.providers.earnings import EarningsCalendar

        seen: dict[str, httpx.Request] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(200, json={"earningsCalendar": []})

        calendar = EarningsCalendar(SECRET, transport=httpx.MockTransport(handler))
        await calendar.get("MU")

        request = seen["request"]
        assert SECRET not in str(request.url)
        assert request.headers.get("X-Finnhub-Token") == SECRET


class TestTheSinksScrubRegardlessOfTheCaller:
    """The defence that covers call sites nobody has audited yet.

    Scrubbing at each caller is what failed here: `agents/supervisor` leaked
    while `market_data/providers/earnings.py`, doing the same job, did not. The
    board and the audit are the two places everything funnels through, so
    that is where the guarantee belongs.
    """

    def test_the_activity_board_scrubs_a_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.activity import BOARD
        from core.config import get_settings

        monkeypatch.setenv("FINNHUB_API_KEY", SECRET)
        get_settings.cache_clear()
        try:
            BOARD.log("scanner", f"Failed: token={SECRET}")
            assert SECRET not in repr(BOARD.events[-1])
        finally:
            get_settings.cache_clear()

    def test_the_activity_board_scrubs_an_agent_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.activity import BOARD

        BOARD.set_agent("scanner", status="error", detail=f"boom token={SECRET}")
        assert SECRET not in repr(BOARD.snapshot())

    @pytest.mark.asyncio
    async def test_the_audit_scrubs_a_nested_payload(self) -> None:
        """Credentials do not always arrive at the top level of a dict."""
        from core.audit import InMemoryAudit

        audit = InMemoryAudit()
        await audit.append(
            "ScanJobFailed",
            "supervisor",
            {"error": f"url?token={SECRET}", "ctx": {"headers": {"X-Finnhub-Token": SECRET}}},
        )

        assert SECRET not in repr(audit.events)

    @pytest.mark.asyncio
    async def test_a_field_named_like_a_credential_is_masked_whatever_it_holds(self) -> None:
        """A random-looking value in a field called `api_key` is still a key."""
        from core.audit import InMemoryAudit

        audit = InMemoryAudit()
        await audit.append("X", "y", {"api_key": "zzz-unremarkable-1234", "symbol": "MU"})

        assert "zzz-unremarkable-1234" not in repr(audit.events)
        assert audit.events[-1]["payload"]["symbol"] == "MU", "ordinary fields survive"


class TestTheSupervisorScrubsWhatItRecords:
    """The three sinks a failed scan writes to, asserted together."""

    @pytest.mark.asyncio
    async def test_a_vendor_failure_leaks_nothing_to_board_or_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agents.supervisor.agent import Supervisor
        from core.activity import BOARD

        logged: list[str] = []
        audited: list[dict] = []

        class _Audit:
            async def append(self, event, actor, payload, **kw):
                audited.append({"event": event, "payload": payload})

        class _Blind:
            """Market data that fails the way Finnhub does."""

            async def get_bars(self, *a, **k):
                raise _vendor_error()

            async def get_last_price(self, *a, **k):
                raise _vendor_error()

        monkeypatch.setattr(BOARD, "log", lambda a, m, **k: logged.append(m))
        monkeypatch.setattr(BOARD, "set_agent", lambda *a, **k: None)

        result = await Supervisor(market_data=_Blind(), audit=_Audit()).scan_symbol("MU")

        assert result.status == "failed"

        for message in logged:
            assert SECRET not in message, f"key leaked to the activity board: {message}"
        for row in audited:
            assert SECRET not in repr(row), f"key leaked to the audit trail: {row}"
        assert SECRET not in repr(result.errors), "key leaked onto the pipeline result"
