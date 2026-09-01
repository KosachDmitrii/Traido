"""Where every name in the universe ended up, with nothing unaccounted for.

The old funnel counted what the loop reached. Names the caps cut off were in no
bucket at all, so "60 considered, 57 no candidate" left three unexplained and
nobody could tell whether they had been rejected, skipped or lost.

At sixty names that is untidy. At a thousand it is the difference between a
scanner you can debug and one you can only restart, because the interesting
question stops being "what did it find" and becomes "why did it not look at the
name I expected".

So this is a *ledger*, and it balances. Every instrument that enters is in
exactly one terminal bucket when the cycle ends, `reconciles()` asserts it, and
a test asserts it over a thousand-name run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ScanFunnel:
    """Stage-by-stage accounting for one cycle.

    Field names follow the pipeline's own stages so a number on the screen can
    be traced to the code that produced it without a translation table.
    """

    # ── What came in ────────────────────────────────────────────────────────
    universe_total: int = 0
    """Instruments the provider offered, before any filter."""

    structurally_eligible: int = 0
    stage0_rejected: int = 0

    # ── Stage 1: cheap market filter ────────────────────────────────────────
    market_filter_evaluated: int = 0
    market_filter_passed: int = 0
    market_filter_rejected: int = 0

    # ── Stage 2: quant pre-ranking ──────────────────────────────────────────
    quant_evaluated: int = 0
    quant_shortlisted: int = 0
    quant_rejected: int = 0
    quant_outranked: int = 0

    # ── Stage 3: deep analysis ──────────────────────────────────────────────
    deep_analysis_outranked: int = 0
    """Shortlisted, then cut by `deep_analysis_top_k` before analysis.

    Counted apart from `quant_outranked`, which is Stage 2's own Top-K cut.
    Both mean "ranked too low", but they answer different questions: one says
    the quant shortlist is the binding limit, the other says the deep cap is,
    and an operator asking why only twenty names were analysed needs to know
    which knob to turn. Merged into one counter — as they were — the deep cap
    could be removed entirely and the number would not move.
    """

    deep_analysis_started: int = 0
    deep_analysis_passed: int = 0
    deep_analysis_failed: int = 0
    deep_analysis_no_candidate: int = 0

    # ── Stage 4: risk ───────────────────────────────────────────────────────
    risk_passed: int = 0
    risk_rejected: int = 0

    # ── Publication ─────────────────────────────────────────────────────────
    published: int = 0
    final_outranked: int = 0
    capacity_rejected: int = 0
    duplicate_symbol_rejected: int = 0

    # ── Things that went wrong rather than being decided ────────────────────
    provider_failed: int = 0
    data_stale: int = 0
    ai_budget_exhausted: int = 0
    position_open: int = 0
    """Never looked at: the book already holds it, so no entry could be acted on.

    Terminal and counted apart from `no candidate`, which means we looked and
    found no setup. Confusing the two is how "we could not look" gets filed as
    "there was nothing there".
    """

    # ── Reasons, for the operator rather than the accountant ────────────────
    stage0_reasons: dict[str, int] = field(default_factory=dict)
    market_filter_reasons: dict[str, int] = field(default_factory=dict)
    quant_reasons: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    # ── Cycle shape ─────────────────────────────────────────────────────────
    paused_on_full_queue: bool = False
    """The cycle stopped because the desk queue was full, not because it finished.

    Without this the two are indistinguishable: a cycle that never looked at a
    single symbol reports the same "0 proposals" as one that scanned everything
    and found nothing.
    """

    def reset(self) -> None:
        fresh = ScanFunnel()
        for key, value in vars(fresh).items():
            setattr(self, key, value)

    # ── The invariant ───────────────────────────────────────────────────────

    def terminal_total(self) -> int:
        """Every instrument, counted exactly once, wherever it stopped.

        Each name falls out at exactly one stage. A name that reaches deep
        analysis is *not* also counted as a Stage 1 pass here — Stage 1's passes
        are accounted for by what Stage 2 did with them, and so on down. So the
        terminal buckets are the rejections at each stage plus the outcomes at
        the end.
        """
        return (
            self.stage0_rejected
            + self.market_filter_rejected
            + self.quant_rejected
            + self.quant_outranked
            + self.deep_analysis_outranked
            + self.ai_budget_exhausted
            + self.deep_analysis_failed
            + self.deep_analysis_no_candidate
            + self.position_open
            + self.provider_failed
            + self.risk_rejected
            + self.published
            + self.final_outranked
            + self.capacity_rejected
            + self.duplicate_symbol_rejected
        )

    def reconciles(self) -> bool:
        """Does the ledger balance against what came in?"""
        return self.terminal_total() == self.universe_total

    def unaccounted(self) -> int:
        return self.universe_total - self.terminal_total()

    def top_rejections(self, n: int = 5) -> list[tuple[str, int]]:
        """Most common first, ties by name.

        The tie-break is not cosmetic: without it the order is the dict's, which
        is the order the reasons happened to first occur, which is the order the
        provider answered in. The operator would see the same cycle described
        two different ways on two runs.
        """
        return sorted(self.rejection_reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:n]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["terminal_total"] = self.terminal_total()
        data["unaccounted"] = self.unaccounted()
        data["reconciles"] = self.reconciles()
        return data
