"""Signal→fill attribution — measure where price/timing deteriorate."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from core.schemas import EntryAttribution


def _ms(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() * 1000.0


def build_attribution(
    *,
    symbol: str,
    opportunity_id: UUID | None = None,
    signal_detected_at: datetime | None = None,
    signal_price: Decimal | None = None,
    candidate_created_at: datetime | None = None,
    candidate_price: Decimal | None = None,
    opportunity_published_at: datetime | None = None,
    published_price: Decimal | None = None,
    operator_approved_at: datetime | None = None,
    approval_price: Decimal | None = None,
    broker_submitted_at: datetime | None = None,
    submit_reference_price: Decimal | None = None,
    broker_filled_at: datetime | None = None,
    fill_price: Decimal | None = None,
    atr: float | None = None,
    expected_60m_move_pct: float | None = None,
) -> EntryAttribution:
    signal_to_fill_bps = None
    signal_to_fill_atr = None
    consumed = None
    remaining = None
    if signal_price is not None and fill_price is not None and signal_price > 0:
        move = float(fill_price - signal_price) / float(signal_price)
        signal_to_fill_bps = move * 10_000.0
        if atr and atr > 0:
            signal_to_fill_atr = float(fill_price - signal_price) / atr
        if expected_60m_move_pct is not None and expected_60m_move_pct != 0:
            consumed = (move * 100.0) / expected_60m_move_pct
            remaining = expected_60m_move_pct - (move * 100.0)

    return EntryAttribution(
        opportunity_id=opportunity_id,
        symbol=symbol.upper(),
        signal_detected_at=signal_detected_at,
        signal_price=signal_price,
        candidate_created_at=candidate_created_at,
        candidate_price=candidate_price,
        opportunity_published_at=opportunity_published_at,
        published_price=published_price,
        operator_approved_at=operator_approved_at,
        approval_price=approval_price,
        broker_submitted_at=broker_submitted_at,
        submit_reference_price=submit_reference_price,
        broker_filled_at=broker_filled_at,
        fill_price=fill_price,
        signal_to_publish_ms=_ms(signal_detected_at, opportunity_published_at),
        publish_to_approval_ms=_ms(opportunity_published_at, operator_approved_at),
        approval_to_submit_ms=_ms(operator_approved_at, broker_submitted_at),
        submit_to_fill_ms=_ms(broker_submitted_at, broker_filled_at),
        signal_to_fill_ms=_ms(signal_detected_at, broker_filled_at),
        signal_to_fill_bps=signal_to_fill_bps,
        signal_to_fill_atr=signal_to_fill_atr,
        expected_60m_move_pct=expected_60m_move_pct,
        remaining_expected_move_at_fill_pct=remaining,
        expected_move_consumed_fraction=consumed,
    )
