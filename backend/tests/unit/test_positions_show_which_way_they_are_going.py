"""An open position on the desk has to say whether it is working.

The list rendered symbol, size, entry, stop and target — everything about the
trade as planned and nothing about how it is doing. Four positions were open on
2026-08-31 and the operator could not tell, from the desk, which of them were up.

The mark is the broker's own valuation, carried on the position it belongs to
rather than fetched separately. What is asserted here is mostly what it must
*not* become: a price that reaches a decision, and a missing price that renders
as zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from api.routes.desk import _mark_to_market
from core.enums import PositionStatus
from core.schemas import Position


def _position(*, avg_entry: str, qty: str, mark: str | None) -> Position:
    return Position(
        id=uuid4(),
        symbol="TEST",
        qty=Decimal(qty),
        avg_entry=Decimal(avg_entry),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(UTC),
        mark=None if mark is None else Decimal(mark),
    )


def test_a_position_in_profit_reports_a_gain() -> None:
    out = _mark_to_market(_position(avg_entry="100", qty="10", mark="101.50"))

    assert out["pnl_pct"] == 1.5
    assert Decimal(out["pnl"]) == Decimal(15)


def test_a_position_in_loss_reports_a_loss() -> None:
    out = _mark_to_market(_position(avg_entry="100", qty="10", mark="98.00"))

    assert out["pnl_pct"] == -2.0
    assert Decimal(out["pnl"]) == Decimal(-20)


def test_an_unpriced_position_reports_nothing_rather_than_zero() -> None:
    """Absent, not flat.

    Zero is a claim — that the position has not moved — and the operator would
    have no way to tell it apart from the broker having gone quiet. The card
    renders a dash for `None` and an arrow for a number, so the distinction
    survives to the screen.
    """
    out = _mark_to_market(_position(avg_entry="100", qty="10", mark=None))

    assert out == {"mark": None, "pnl": None, "pnl_pct": None}


def test_the_mark_is_absent_by_default() -> None:
    """A broker that does not report one must not inherit somebody else's."""
    position = Position(
        id=uuid4(),
        symbol="TEST",
        qty=Decimal(10),
        avg_entry=Decimal(100),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(UTC),
    )

    assert position.mark is None


def test_no_gate_reads_the_mark() -> None:
    """It carries no age and no source, so it cannot satisfy a freshness check.

    Every price-consuming gate on this desk refuses a quote it cannot date. The
    mark is a display figure that would silently pass as one, so the boundary is
    kept here rather than in a comment: risk, the gates and the exit rules do
    not mention it.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    forbidden = [
        root / "risk" / "risk_engine.py",
        root / "trading" / "gates.py",
        root / "agents" / "position" / "agent.py",
    ]

    for path in forbidden:
        assert ".mark" not in path.read_text(), f"{path.name} reads the display mark"
