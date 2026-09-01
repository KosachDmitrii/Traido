"""Prove each scanner regression test fails when its fix is removed.

A test that has never been red is a test nobody has checked. This reverts one
guarantee at a time — by editing the source, running only the test that is meant
to catch it, and restoring the file — and reports RED or, worse, GREEN.

A GREEN line here is the interesting one: it means the test passes with the bug
present and is therefore not testing what its name claims.

    PYTHONPATH=. python scripts/red_without_fix.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Regression:
    """One guarantee, the edit that removes it, and the test that should notice."""

    name: str
    path: str
    before: str
    after: str
    test: str


REGRESSIONS = [
    Regression(
        name="static ~60-symbol cap",
        path="agents/scanner/cycle.py",
        before="    result.universe_symbols = snapshot.symbols\n",
        after="    result.universe_symbols = snapshot.symbols\n    snapshot.eligible = snapshot.eligible[:60]\n",
        test="tests/unit/test_scanner_cycle_coverage.py::test_a_universe_larger_than_the_old_cap_is_not_truncated",
    ),
    Regression(
        name="deep pipeline on every shortlisted symbol",
        path="agents/scanner/cycle.py",
        before="    deep_cap = max(0, settings.deep_analysis_top_k)",
        after="    deep_cap = 10_000  # the old behaviour: analyse everything that got here",
        test="tests/integration/test_scanner_scale.py::test_only_finalists_reach_the_expensive_stage",
    ),
    Regression(
        name="Stage 1 runs the full pipeline instead of a batch read",
        path="agents/scanner/cycle.py",
        before="    stage1 = apply_market_filter(",
        after="    funnel.market_filter_evaluated = 0\n    stage1 = apply_market_filter(",
        test="tests/unit/test_scanner_cycle_coverage.py::test_the_whole_universe_is_covered_in_one_cycle",
    ),
    Regression(
        name="ranking follows the quant shortlist, not conviction",
        path="agents/scanner/cycle.py",
        before="    ranked = sorted(passed, key=rank_key)",
        after="    ranked = list(passed)  # whatever order the workers finished in",
        test="tests/integration/test_scanner_scale.py::test_publication_order_follows_conviction_not_the_quant_shortlist",
    ),
    Regression(
        name="no stable tie-break in the rank key",
        path="agents/scanner/cycle.py",
        before="    return (-candidate.confidence, -candidate.risk_reward, candidate.symbol.upper())",
        after="    return (-candidate.confidence, -candidate.risk_reward, '')",
        test="tests/unit/test_scanner_ranking.py::test_two_identical_candidates_are_still_totally_ordered",
    ),
    Regression(
        name="results reordered by completion rather than input",
        path="core/concurrency.py",
        before="        return list(await asyncio.gather(*(_one(item) for item in materialised)))",
        after="        out = []\n        for coro in asyncio.as_completed([_one(i) for i in materialised]):\n            out.append(await coro)\n        return out",
        test="tests/unit/test_scanner_infra.py::test_results_keep_input_order_not_completion_order",
    ),
    Regression(
        name="portfolio queried per symbol",
        path="trading/scan_context.py",
        before="        async with self._portfolio_lock:\n            if self._portfolio is None or refresh:",
        after="        if True:\n            if self._portfolio is None or refresh:\n                await asyncio.sleep(0)",
        test="tests/integration/test_scanner_scale.py::test_the_broker_account_is_read_once_for_the_whole_universe",
    ),
    Regression(
        name="capacity reserved during analysis",
        path="agents/scanner/cycle.py",
        before="    ranked = sorted(passed, key=rank_key)",
        after="    ranked = sorted(passed, key=lambda r: r.candidate.symbol)",
        test="tests/integration/test_scanner_scale.py::test_capacity_is_spent_after_ranking_not_during_analysis",
    ),
    Regression(
        name="a failed candidate disappears from the funnel",
        path="agents/scanner/cycle.py",
        before="            funnel.deep_analysis_failed += 1\n            BOARD.log(",
        after="            BOARD.log(",
        test="tests/integration/test_scanner_scale.py::test_the_funnel_still_balances_when_things_go_wrong",
    ),
    Regression(
        name="the deep cap's cut is filed as a Stage 2 cut",
        path="agents/scanner/cycle.py",
        before="            funnel.deep_analysis_outranked += 1",
        after="            funnel.quant_outranked += 1",
        test="tests/integration/test_scanner_scale.py::test_only_finalists_reach_the_expensive_stage",
    ),
    Regression(
        name="unbounded concurrency",
        path="core/concurrency.py",
        before="                semaphore=asyncio.Semaphore(max(1, budget.max_concurrency)),",
        after="                semaphore=asyncio.Semaphore(10_000),",
        test="tests/integration/test_scanner_scale.py::test_concurrency_stays_within_its_budget",
    ),
    Regression(
        name="one symbol's failure kills the scan",
        path="core/concurrency.py",
        before="            except Exception as exc:  # noqa: BLE001\n                return exc",
        after="            except Exception:\n                raise",
        test="tests/integration/test_scanner_scale.py::test_one_symbol_failing_does_not_kill_the_scan",
    ),
    Regression(
        name="missing data defaults to zero instead of rejecting",
        path="agents/scanner/prefilter.py",
        before="    if price is None:\n        reasons.append(MarketFilterReason.MISSING_PRICE)",
        after="    if price is None:\n        price = Decimal(0)",
        test="tests/unit/test_scanner_stages.py::test_bad_data_is_rejected_rather_than_defaulted",
    ),
    Regression(
        name="a stale cache entry is served after a policy change",
        path="core/freshness.py",
        before="        if input_version != self.input_version:\n            return False",
        after="        if False:\n            return False",
        test="tests/unit/test_scanner_infra.py::test_a_value_computed_under_different_inputs_is_wrong_not_merely_stale",
    ),
    Regression(
        name="the AI budget drops candidates without recording them",
        path="core/concurrency.py",
        before="            self.exhausted_candidates.append(symbol)\n            return False",
        after="            return False",
        test="tests/integration/test_scanner_scale.py::test_an_exhausted_ai_budget_shortens_the_list_deterministically",
    ),
]


def _run(test: str) -> bool:
    """True if the test passed."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "-p", "no:randomly", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,  # a failing test is the expected result here, not an error
    )
    return proc.returncode == 0


def main() -> int:
    failures: list[str] = []
    print(f"{'guarantee':<48} {'test':<12} verdict")
    print("-" * 78)

    for reg in REGRESSIONS:
        path = ROOT / reg.path
        original = path.read_text()
        if reg.before not in original:
            print(f"{reg.name:<48} {'—':<12} SKIPPED (anchor not found)")
            failures.append(reg.name)
            continue

        path.write_text(original.replace(reg.before, reg.after, 1))
        try:
            passed = _run(reg.test)
        finally:
            path.write_text(original)

        verdict = "GREEN — test does not catch it" if passed else "RED"
        print(f"{reg.name:<48} {'broken':<12} {verdict}")
        if passed:
            failures.append(reg.name)

    print()
    if failures:
        print(f"{len(failures)} regression(s) not caught: {', '.join(failures)}")
        return 1
    print(f"All {len(REGRESSIONS)} regressions turn the suite red when the fix is removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
