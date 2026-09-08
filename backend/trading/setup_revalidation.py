"""Rebuild price-sensitive setup evidence when an EntryWatch triggers."""

from __future__ import annotations

from collections.abc import Mapping

from core.schemas import SetupQualityBreakdown


def merge_revalidated_setup(
    fresh: SetupQualityBreakdown,
    persisted: Mapping[str, int] | None,
) -> SetupQualityBreakdown:
    """Use fresh market structure while retaining unavailable catalyst evidence.

    Revalidation has fresh bars, quote and market context, so trend, impulse,
    retracement, volume, support, market alignment and liquidity must all be
    recalculated.  The watch loop does not fetch a second news assessment;
    retain only the candidate's already validated catalyst component until the
    final pre-trade path performs its own fresh event/news checks.
    """
    if not persisted:
        return fresh
    catalyst = persisted.get("catalyst")
    if isinstance(catalyst, bool) or not isinstance(catalyst, int):
        return fresh
    if not 0 <= catalyst <= 100:
        return fresh
    return fresh.model_copy(update={"catalyst": catalyst})
