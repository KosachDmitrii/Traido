"""Live agent activity feed for the desk UI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from core.redaction import redact_secrets

ACTIVE_WINDOW_SEC = 6.0
"""How long after doing work an agent still counts as active in the current pass.

The desk samples this board every five seconds, but most stages finish in far
less than that: scoring structure is pure computation, and even the network-bound
analysts return in a few hundred milliseconds. Only the scanner, which owns the
bar fetch, is ever caught mid-stint. Reporting a bare `status == "working"` to a
five-second poll therefore means the analysts are permanently invisible — they do
run, and the desk simply never looks at the right microsecond.

This window must stay longer than the poll interval, or a stint can still fall
entirely between two samples.
"""


@dataclass
class AgentState:
    id: str
    name: str
    status: str = "idle"  # idle | working | done | error
    detail: str = ""
    last_symbol: str | None = None
    score: int | float | None = None
    updated_at: str | None = None
    last_worked_at: float | None = None
    """Monotonic time of the most recent `working`. Drives `active`."""


@dataclass
class ActivityEvent:
    ts: str
    agent: str
    message: str
    symbol: str | None = None
    level: str = "info"


class AgentActivityBoard:
    def __init__(self) -> None:
        self._lock = Lock()
        self.agents: dict[str, AgentState] = {
            "scanner": AgentState("scanner", "Scanner"),
            "technical": AgentState("technical", "Technical"),
            "news": AgentState("news", "News"),
            "market": AgentState("market", "Market"),
            "strategy": AgentState("strategy", "Strategy"),
            "risk": AgentState("risk", "Risk Engine"),
            "review": AgentState("review", "Review"),
            "position": AgentState("position", "Position"),
        }
        self.events: list[ActivityEvent] = []

    def set_agent(
        self,
        agent_id: str,
        *,
        status: str,
        detail: str = "",
        symbol: str | None = None,
        score: float | None = None,
    ) -> None:
        with self._lock:
            agent = self.agents[agent_id]
            agent.status = status
            agent.detail = redact_secrets(detail)
            if symbol is not None:
                agent.last_symbol = symbol
            if score is not None:
                agent.score = score
            agent.updated_at = datetime.now(UTC).isoformat()
            if status == "working":
                agent.last_worked_at = time.monotonic()

    def log(
        self,
        agent: str,
        message: str,
        *,
        symbol: str | None = None,
        level: str = "info",
    ) -> None:
        with self._lock:
            self.events.append(
                ActivityEvent(
                    ts=datetime.now(UTC).isoformat(),
                    agent=agent,
                    message=redact_secrets(message),
                    symbol=symbol,
                    level=level,
                )
            )
            self.events = self.events[-200:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "agents": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "status": a.status,
                        "detail": a.detail,
                        "last_symbol": a.last_symbol,
                        "score": a.score,
                        "updated_at": a.updated_at,
                        "active": (
                            a.last_worked_at is not None
                            and (now - a.last_worked_at) <= ACTIVE_WINDOW_SEC
                        ),
                    }
                    for a in self.agents.values()
                ],
                "events": [
                    {
                        "ts": e.ts,
                        "agent": e.agent,
                        "message": e.message,
                        "symbol": e.symbol,
                        "level": e.level,
                    }
                    for e in list(self.events)[-200:]
                ],
            }


BOARD = AgentActivityBoard()
