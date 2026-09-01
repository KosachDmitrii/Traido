"""
Telegram notifications.

The desk only creates opportunities that a human must confirm, so a proposal
nobody sees is a proposal that expires. This is the out-of-band channel.

Notifications are strictly one-way in V1: Traido sends, the human decides in
the UI. Nothing here can approve, size, or place an order — accepting trade
commands over a chat bot would put the capital path behind a channel with no
authentication worth the name.

Every failure is swallowed and logged. A notification outage must never break
a scan cycle or, worse, an execution path.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

import httpx

from core.schemas import TradeOpportunity

TELEGRAM_API = "https://api.telegram.org"
REQUEST_TIMEOUT = 8.0
MAX_MESSAGE_CHARS = 3500


@dataclass(frozen=True)
class NotifyResult:
    sent: bool
    detail: str = ""


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self._token = bot_token
        self._chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(self, text: str) -> NotifyResult:
        if not self.configured:
            return NotifyResult(sent=False, detail="telegram_not_configured")

        payload = {
            "chat_id": self._chat_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return NotifyResult(sent=False, detail=f"telegram_failed: {type(exc).__name__}")
        return NotifyResult(sent=True)

    async def send_opportunity(self, opp: TradeOpportunity) -> NotifyResult:
        return await self.send(format_opportunity(opp))

    async def send_kill_switch(self, *, enabled: bool, actor: str) -> NotifyResult:
        state = "ENGAGED" if enabled else "released"
        return await self.send(
            f"<b>Kill switch {state}</b>\nby {html.escape(actor)}\n"
            + ("All new entries are blocked." if enabled else "Scanning resumes.")
        )

    async def send_risk_halt(self, reasons: list[str]) -> NotifyResult:
        listed = "\n".join(f"• {html.escape(r)}" for r in reasons)
        return await self.send(f"<b>Trading halted by risk limits</b>\n{listed}")


def format_opportunity(opp: TradeOpportunity) -> str:
    """Human-readable proposal. Numbers first — the reasoning is secondary."""
    c = opp.candidate
    risk_per_share = c.entry - c.stop
    qty = opp.risk.sized_qty
    max_loss = opp.risk.max_loss_usd

    lines = [
        f"<b>{html.escape(c.symbol)}</b> — BUY proposal",
        f"Entry <code>{c.entry}</code>  Stop <code>{c.stop}</code>  Target <code>{c.target}</code>",
        f"R:R <b>{c.risk_reward:.2f}</b>  ·  risk/share <code>{risk_per_share}</code>",
    ]
    if qty is not None:
        lines.append(
            f"Size <b>{qty}</b> shares"
            + (f"  ·  max loss <code>${max_loss}</code>" if max_loss else "")
        )
    if c.technical_score is not None:
        lines.append(f"Technical {c.technical_score}/100")

    reasons = [html.escape(r) for r in c.reasons[:4]]
    if reasons:
        lines.append("")
        lines.extend(f"• {r}" for r in reasons)

    lines.append("")
    lines.append("Confirm or skip in the Traido desk. This message cannot approve a trade.")
    return "\n".join(lines)


_NOTIFIER: TelegramNotifier | None = None
_NOTIFIER_KEY: tuple[str | None, str | None] | None = None


def get_notifier(bot_token: str | None, chat_id: str | None) -> TelegramNotifier:
    global _NOTIFIER, _NOTIFIER_KEY
    key = (bot_token, chat_id)
    if _NOTIFIER is None or _NOTIFIER_KEY != key:
        _NOTIFIER = TelegramNotifier(bot_token, chat_id)
        _NOTIFIER_KEY = key
    return _NOTIFIER
