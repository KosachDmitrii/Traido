"""Keep credentials out of anything durable or on screen.

Vendor errors are the usual carrier. Finnhub authenticates by query parameter,
so `httpx` puts the key in the request URL and `HTTPStatusError` puts the URL in
its message — and a desk that logs `str(exc)` has then printed a live API key to
the activity feed and written it into the audit table. That happened here, on
every Finnhub 503, which on the free tier is often.

The real fix is upstream: send credentials in headers, which the Finnhub callers
now do. This module is the net under it, because the fix only covers the vendors
we have already thought about, and a leaked key is not a bug that can be undone
by a later patch — the key has to be rotated, and one nobody notices leaking
never is.

Two passes, because either alone misses cases the other catches:

- **By value.** The secrets this process actually holds, searched for literally.
  Catches a key echoed back in a JSON error body or rendered in a traceback,
  which no pattern would recognise.
- **By shape.** `token=`, `api_key=`, `Authorization:` and friends. Catches
  credentials this process does not know about — another tenant's key in a
  proxied response, or a vendor we add tomorrow.

Redaction is deliberately narrow. Scrubbing so eagerly that `503` or the
hostname disappears would hide the diagnosis along with the secret, and an
operator who cannot see what failed will turn the redaction off.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

_MIN_SECRET_LEN = 8
"""Below this a "secret" is not distinctive enough to search for.

An unset key is the empty string, and `"" in text` is true everywhere, so
without a floor this would replace every character of every message. Short
values are also likely to be substrings of ordinary words.
"""

_CREDENTIAL_PARAM = re.compile(
    r"(?i)\b(token|api[-_]?key|access[-_]?token|auth|secret|password|passwd|pwd|key)"
    r"(=|%3D)([^&\s'\"]{4,})"
)
"""A credential in a query string, in either raw or percent-encoded form.

`key` is last and deliberately unanchored on the right, so `sortkey=name`
matches too. Losing a sort order from a log line costs nothing; keeping a
credential costs a rotation.
"""

_CREDENTIAL_HEADER = re.compile(
    r"(?i)\b(authorization|x-[a-z0-9-]*(?:token|key)|api[-_]?key)"
    r"(\s*[:=]\s*)(?:Bearer\s+|Basic\s+)?([^\s,'\"]{4,})"
)
"""The same thing in a rendered header dict or an HTTP trace."""


def configured_secrets() -> set[str]:
    """Every credential this process holds, as literal strings.

    Read live rather than cached at import: tests and rotations both change
    them, and a redactor working from a stale list is a redactor that stops
    working exactly when a key changes.
    """
    try:
        from core.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001
        # Redaction must never be the reason a log line fails to be written.
        return set()

    candidates = (
        getattr(settings, name, None)
        for name in (
            "finnhub_api_key",
            "fred_api_key",
            "alpaca_api_key",
            "alpaca_api_secret",
            "anthropic_api_key",
            "telegram_bot_token",
            "api_key",
        )
    )
    return {v for v in candidates if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN}


def redact_secrets(text: str) -> str:
    """Return `text` with any credential replaced by `MASK`.

    Safe to call on anything, including text with no secret in it, and cheap
    enough for a log path.
    """
    if not text:
        return text

    for secret in configured_secrets():
        text = text.replace(secret, MASK)

    text = _CREDENTIAL_PARAM.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    return _CREDENTIAL_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)


_SECRET_FIELD = re.compile(r"(?i)(token|api[-_]?key|secret|password|passwd|pwd|credential|auth)")
"""A key whose *value* is a credential regardless of what the value looks like.

A random-looking key is invisible to `redact_secrets`, which needs either a
known value or a `name=value` shape. A field called `api_key` announces itself.
"""


def redact_payload(value: object) -> object:
    """Walk a JSON-shaped structure, scrubbing credentials wherever they sit.

    Used on audit payloads, which are the most durable thing this process
    writes — a log line rotates away, an audit row does not.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {
            k: MASK if isinstance(k, str) and _SECRET_FIELD.search(k) else redact_payload(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(v) for v in value)
    return value


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """`redact_payload` for the common case, with the type preserved."""
    return {
        key: MASK if isinstance(key, str) and _SECRET_FIELD.search(key) else redact_payload(value)
        for key, value in payload.items()
    }
