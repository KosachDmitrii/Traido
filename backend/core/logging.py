"""
Structured logging.

Trading systems are debugged after the fact from logs, so the logs have to be
machine-readable and carry the fields you will actually want to filter on:
symbol, pipeline run, agent, and decision. JSON in deployed environments,
human-readable text locally.

Secrets never reach a log line: `redact()` scrubs known credential keys, and
the formatter applies it to every extra field.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

SECRET_HINTS = ("key", "secret", "token", "password", "authorization", "cookie")
REDACTED = "***"

_STANDARD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def redact(value: Any, key: str = "") -> Any:
    """Replace anything that looks like a credential with a placeholder."""
    if any(hint in key.lower() for hint in SECRET_HINTS):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = redact(value, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s — %(message)s", "%H:%M:%S")


def configure_logging(*, level: str | None = None, json_output: bool | None = None) -> None:
    """Install the root handler. Idempotent — safe to call from tests and lifespan."""
    resolved_level = (level or os.getenv("TRAIDO_LOG_LEVEL") or "INFO").upper()
    if json_output is None:
        env = (os.getenv("TRAIDO_ENV") or "development").lower()
        json_output = env not in {"development", "dev", "local", "test", "testing"}

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # Uvicorn installs its own noisy handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.LoggerAdapter[logging.Logger]:
    """Logger that accepts structured context via `extra=`."""
    return logging.LoggerAdapter(logging.getLogger(name), {})
