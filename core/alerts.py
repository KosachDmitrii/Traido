"""
Operator alerts — out-of-band, fail-soft.

The dashboard is not a monitoring system. CRITICAL conditions (UNKNOWN,
ProtectionMissing / Unverified, stale reconciliation, kill switch) must reach
an operator who is not looking at the desk.

Alerts are never on the capital path: a Telegram outage must not refuse a trade
or block reconciliation. Deduplicate by key and rate-limit so a stuck UNKNOWN
does not become a flood.
"""

from __future__ import annotations

import html
import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SEC = 300.0


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    INFO = "info"


@dataclass(frozen=True)
class Alert:
    key: str
    severity: AlertSeverity
    title: str
    body: str = ""


class AlertBus:
    """In-process alert bus with cooldown deduplication."""

    def __init__(self, *, cooldown_sec: float = DEFAULT_COOLDOWN_SEC) -> None:
        self._cooldown = cooldown_sec
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self._emitted: list[Alert] = []

    def reset(self) -> None:
        with self._lock:
            self._last_sent.clear()
            self._emitted.clear()

    def should_emit(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < self._cooldown:
                return False
            self._last_sent[key] = now
            return True

    async def emit(self, alert: Alert) -> bool:
        """Return True if the alert was newly emitted (not suppressed)."""
        if not self.should_emit(alert.key):
            return False
        with self._lock:
            self._emitted.append(alert)
        logger.warning(
            "alert %s [%s] %s — %s",
            alert.key,
            alert.severity.value,
            alert.title,
            alert.body,
        )
        try:
            await self._deliver(alert)
        except Exception:  # noqa: BLE001 — never break the caller
            logger.exception("alert delivery failed for %s", alert.key)
        return True

    async def _deliver(self, alert: Alert) -> None:
        from core.config import get_settings
        from notifications.telegram import get_notifier

        settings = get_settings()
        notifier = get_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
        if not notifier.configured:
            return
        text = (
            f"<b>{html.escape(alert.severity.value.upper())}</b> "
            f"{html.escape(alert.title)}\n"
            f"{html.escape(alert.body)}"
        )
        await notifier.send(text)

    def recent(self) -> list[Alert]:
        with self._lock:
            return list(self._emitted)


ALERTS = AlertBus()


async def alert_if(
    *,
    key: str,
    severity: AlertSeverity,
    title: str,
    body: str = "",
    condition: bool,
) -> bool:
    if not condition:
        return False
    return await ALERTS.emit(Alert(key=key, severity=severity, title=title, body=body))
