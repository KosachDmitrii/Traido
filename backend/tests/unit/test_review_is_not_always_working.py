"""Serving a read is not doing work, and must not read as work on the board.

The desk rebuilds the review report on every poll — that is how the panel shows
a live trade count. But the report builder also announced itself as `working`,
so the activity window was re-stamped several times a minute and the Review row
animated continuously, claiming a pass was under way whenever a browser tab was
open. `announce` already marked the difference between a pass and a snapshot;
only the log honoured it.
"""

from __future__ import annotations

from sqlalchemy import create_engine

from agents.review.agent import build_review
from core.activity import BOARD
from database.session import init_db


def _review_row() -> dict:
    return next(a for a in BOARD.snapshot()["agents"] if a["id"] == "review")


def _empty_journal(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'journal.db'}", future=True)
    init_db(eng)
    return eng


def test_a_desk_poll_does_not_leave_review_looking_busy(tmp_path) -> None:
    """What the desk does on a five-second timer, repeatedly."""
    eng = _empty_journal(tmp_path)

    for _ in range(3):
        build_review(live_only=True, announce=False, engine=eng)

    row = _review_row()
    assert row["active"] is False, "polling the desk makes Review animate forever"
    assert row["status"] != "working"


def test_an_explicit_review_run_is_still_visible(tmp_path) -> None:
    """The gate must not silence a real run — that would be the opposite bug."""
    eng = _empty_journal(tmp_path)

    build_review(live_only=True, announce=True, engine=eng)

    assert _review_row()["active"] is True
