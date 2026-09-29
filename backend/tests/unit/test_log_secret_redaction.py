"""Vendor URLs and access paths must not disclose credentials in runtime logs."""

from __future__ import annotations

import json
import logging

from core.logging import JsonFormatter, TextFormatter


def test_formatters_redact_fred_url_and_access_key() -> None:
    secret = "test-secret-value-12345"
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        f"GET https://api.stlouisfed.org/fred/series/observations?api_key={secret} "
        f"and /api/v1/desk/stream?api_key={secret}",
        (),
        None,
    )
    record.vendor_detail = f"api_key={secret}"
    record.vendor_context = {"url": f"https://example.org/?api_key={secret}"}
    for output in (TextFormatter().format(record), JsonFormatter().format(record)):
        assert secret not in output
        assert "api_key=[REDACTED]" in output
    assert secret not in json.dumps(json.loads(JsonFormatter().format(record)))
