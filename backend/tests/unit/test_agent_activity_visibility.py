"""An agent that runs must be visible as running to a desk that polls.

The board is sampled, not streamed. Most pipeline stages finish in far less time
than the gap between two desk polls — scoring structure is pure computation, and
even the network-bound analysts return in a few hundred milliseconds. Only the
scanner, which owns the bar fetch, is ever caught mid-stint by a five-second
sample. So a bare `status == "working"` renders every analyst as permanently
still, which is not what happened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.activity import ACTIVE_WINDOW_SEC, AgentActivityBoard


def _agent(board: AgentActivityBoard, agent_id: str) -> dict:
    return next(a for a in board.snapshot()["agents"] if a["id"] == agent_id)


def test_the_active_window_outlasts_the_desk_poll_interval() -> None:
    """Otherwise a whole stint can fall between two samples and go unseen.

    The interval is read out of the desk client rather than restated here,
    because the two numbers only mean anything relative to each other. Slowing
    the poll down past the window would silently undo this whole mechanism.
    """
    source = (
        Path(__file__).resolve().parents[3] / "frontend/src/context/DeskContext.tsx"
    ).read_text()
    intervals = [int(ms) for ms in re.findall(r"^const LIGHT\w*_MS = (\d+);", source, re.MULTILINE)]
    assert intervals, "desk poll interval not found — did DeskContext.tsx move?"

    assert ACTIVE_WINDOW_SEC > max(intervals) / 1000


def test_a_stage_that_finishes_instantly_is_still_reported_as_active() -> None:
    """The technical agent scores and returns without ever awaiting anything.

    Between `working` and `done` there is no suspension point at all, so no poll
    can observe the intermediate status. It did run, and the desk has to say so.
    """
    board = AgentActivityBoard()
    board.set_agent("technical", status="working", detail="Scoring structure", symbol="AAPL")
    board.set_agent("technical", status="done", detail="bearish", symbol="AAPL", score=24)

    tech = _agent(board, "technical")
    assert tech["status"] == "done"
    assert tech["active"] is True, "an agent that just ran reads as idle to the desk"


def test_an_agent_that_never_ran_is_not_active() -> None:
    board = AgentActivityBoard()
    assert _agent(board, "position")["active"] is False


def test_activity_decays_once_the_pass_moves_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The border has to stop. `active` is 'in this pass', not 'ran once'."""
    board = AgentActivityBoard()
    clock = [1_000.0]
    monkeypatch.setattr("core.activity.time.monotonic", lambda: clock[0])

    board.set_agent("news", status="working", detail="Reading headlines", symbol="AAPL")
    board.set_agent("news", status="done", detail="neutral", symbol="AAPL", score=50)
    assert _agent(board, "news")["active"] is True

    clock[0] += ACTIVE_WINDOW_SEC + 0.1
    assert _agent(board, "news")["active"] is False


def test_a_non_working_status_does_not_extend_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only work counts. A `done` or `error` write must not refresh the window,
    or an agent parked on its last result would animate forever."""
    board = AgentActivityBoard()
    clock = [1_000.0]
    monkeypatch.setattr("core.activity.time.monotonic", lambda: clock[0])

    board.set_agent("market", status="working", detail="Regime check")
    clock[0] += ACTIVE_WINDOW_SEC + 0.1
    board.set_agent("market", status="done", detail="neutral", score=50)

    assert _agent(board, "market")["active"] is False
