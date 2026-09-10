"""Persisted ORB selection and observation for one market session.

Legacy funnel field names remain in the status API for historical telemetry only.
Selection itself uses complete opening ranges and relative volume.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from agents.scanner.funnel import ScanFunnel
from core.activity import BOARD
from core.config import Settings, get_settings
from core.redaction import redact_secrets
from core.schemas import PipelineResult
from trading.scan_context import ScanContext, open_scan_context
from universe.service import UniverseService

logger = logging.getLogger(__name__)


def _trace(ctx: ScanContext, stage: str, **facts: object) -> None:
    message = redact_secrets(
        json.dumps({"scan_id": str(ctx.scan_id), "stage": stage, **facts}, default=str)
    )
    logger.info("ScannerTrace %s", message)
    BOARD.log("scanner", f"ScannerTrace {message}")


@dataclass
class StageTimings:
    """Seconds by stage. The first question about a slow cycle is always where."""

    universe: float = 0.0
    market_filter: float = 0.0
    prerank: float = 0.0
    deep_analysis: float = 0.0
    publish: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in vars(self).items()}


@dataclass
class CycleResult:
    """Everything one cycle produced, for the status object and the tests."""

    funnel: ScanFunnel = field(default_factory=ScanFunnel)
    timings: StageTimings = field(default_factory=StageTimings)
    published: list[str] = field(default_factory=list)
    shortlist: list[str] = field(default_factory=list)
    deep_symbols: list[str] = field(default_factory=list)
    coverage: dict[str, str | int] = field(default_factory=dict)
    universe_symbols: list[str] = field(default_factory=list)
    provider_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    ai_budget: dict[str, float | int] = field(default_factory=dict)
    error: str | None = None


def rank_key(result: PipelineResult) -> tuple[float, float, str]:
    rv = float(result.candidate.orb_plan.get("relative_volume", 0)) if result.candidate else 0.0
    return (-rv, 0.0, result.symbol)


async def run_cycle(
    *,
    settings: Settings | None = None,
    universe_service: UniverseService,
    timeframes: tuple[object, ...] = (),
    max_open: int = 20,
    scheduled_at: datetime | None = None,
    context: ScanContext | None = None,
    on_progress: Callable[[ScanFunnel], None] | None = None,
) -> CycleResult:
    from strategy.orb.runtime import discover, observe, retire_pending_legacy

    result = CycleResult()
    if on_progress:
        on_progress(result.funnel)
    started = time.monotonic()
    retire_pending_legacy()
    if context is None:
        async with open_scan_context(settings or get_settings()) as ctx:
            data = await discover(ctx, universe_service)
            statuses = await observe(context=ctx) if data.get("status") == "ready" else {}
    else:
        data = await discover(context, universe_service)
        statuses = await observe(context=context) if data.get("status") == "ready" else {}
    counts = data.get("counts", {})
    f = result.funnel
    f.universe_total = counts.get("universe", 0)
    f.structurally_eligible = counts.get("eligible", 0)
    f.stage0_rejected = f.universe_total - f.structurally_eligible
    f.market_filter_passed = counts.get("qualified", 0)
    f.quant_shortlisted = counts.get("selected", 0)
    f.deep_analysis_started = f.quant_shortlisted
    f.deep_analysis_completed = f.quant_shortlisted
    f.market_filter_rejected = f.structurally_eligible - f.market_filter_passed
    f.quant_outranked = f.market_filter_passed - f.quant_shortlisted
    f.rejection_reasons = data.get("rejection_counts", {})
    result.shortlist = list(data.get("plans", {}))
    result.deep_symbols = result.shortlist
    result.universe_symbols = result.shortlist
    if data.get("status") == "ready":
        f.published = statuses.get("awaiting_confirmation", 0)
        f.risk_passed = f.published
        f.wait_for_entry = statuses.get("wait_for_entry", 0)
        f.data_blocked = statuses.get("data_blocked", 0)
        f.risk_rejected = statuses.get("risk_rejected", 0)
        f.position_open = statuses.get("position_open", 0)
        f.deep_analysis_no_candidate = (
            f.quant_shortlisted
            - f.published
            - f.wait_for_entry
            - f.data_blocked
            - f.risk_rejected
            - f.position_open
        )
    else:
        result.error = data.get("reason")
    result.timings.total = time.monotonic() - started
    return result
