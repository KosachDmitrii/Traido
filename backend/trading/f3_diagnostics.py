"""F3 diagnostics aggregates — signal / wait / target / shadow effectiveness."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from trading.entry_watches import ENTRY_WATCHES
from trading.historical_mfe import sample_counts

ROOT = Path(__file__).resolve().parents[1]
SHADOW_PATH = ROOT / "data" / "shadow_entry_policy.jsonl"
REPORT_PATH = ROOT / "data" / "f3_forward_report.json"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def build_f3_diagnostics() -> dict:
    shadows = _read_jsonl(SHADOW_PATH)
    old = Counter(r.get("old_policy") for r in shadows)
    new = Counter(r.get("new_policy") for r in shadows)
    cohorts = Counter(r.get("session_cohort") for r in shadows)

    wait_instead_of_buy = sum(
        1
        for r in shadows
        if r.get("old_policy") == "buy_now" and r.get("new_policy") == "wait_for_entry"
    )
    no_trade_instead = sum(
        1
        for r in shadows
        if r.get("old_policy") == "buy_now" and r.get("new_policy") == "no_trade"
    )
    both_buy = sum(
        1 for r in shadows if r.get("old_policy") == "buy_now" and r.get("new_policy") == "buy_now"
    )

    open_watches = ENTRY_WATCHES.list_open()
    watch_status = ENTRY_WATCHES.status_counts()

    qualities = [r.get("entry_quality") for r in shadows if r.get("entry_quality") is not None]
    avg_quality = round(sum(qualities) / len(qualities), 1) if qualities else None

    chase = Counter()
    for r in shadows:
        for code in r.get("chase_reasons") or []:
            chase[code] += 1

    rth_n = cohorts.get("rth", 0)
    target_rth = 100
    mfe = sample_counts()

    return {
        "signal_quality": {
            "shadow_samples": len(shadows),
            "avg_entry_quality": avg_quality,
            "session_cohorts": dict(cohorts),
            "old_policy_counts": dict(old),
            "new_policy_counts": dict(new),
            "top_chase_reasons": chase.most_common(8),
        },
        "execution_timing": {
            "note": "Per-fill attribution is on OpportunityApproved audit payloads",
            "shadow_samples": len(shadows),
        },
        "target_quality": {
            "historical_mfe_samples": mfe,
            "min_samples_for_reachability": 30,
            "reachability_ready": mfe.get("total", 0) >= 30,
        },
        "wait_effectiveness": {
            "open_watches": len(open_watches),
            "watch_status_counts": watch_status,
            "old_buy_to_wait": wait_instead_of_buy,
            "old_buy_to_no_trade": no_trade_instead,
            "both_buy_now": both_buy,
            "wait_rate_vs_old_buy_pct": round(
                100.0
                * wait_instead_of_buy
                / max(1, wait_instead_of_buy + no_trade_instead + both_buy),
                1,
            )
            if shadows
            else None,
        },
        "forward_paper": {
            "note": (
                "Do not claim FIXED until ~100 new RTH shadow samples accumulate under F3"
            ),
            "shadow_samples": len(shadows),
            "rth_shadow_samples": rth_n,
            "target_rth_samples": target_rth,
            "progress_pct": round(100.0 * min(rth_n, target_rth) / target_rth, 1),
            "claim_fixed": False if rth_n < target_rth else None,
        },
    }


def write_forward_report(path: Path | None = None) -> dict:
    """Persist latest F3 diagnostics for continuous Paper measurement."""
    payload = build_f3_diagnostics()
    out = path or REPORT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    wrapped = {"generated_at": datetime.now(UTC).isoformat(), **payload}
    out.write_text(json.dumps(wrapped, indent=2) + "\n", encoding="utf-8")
    return wrapped
