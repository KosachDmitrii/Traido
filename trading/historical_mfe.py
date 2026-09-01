"""Historical MFE samples for TargetReachability (F3).

Persists realized MFE% for comparable setups so adaptive targets can say
REALISTIC / AMBITIOUS with a sample size, not invent a probability.
"""

from __future__ import annotations

import json
import statistics
import threading
from datetime import UTC, datetime
from pathlib import Path

_LOCK = threading.Lock()
_PATH = Path(__file__).resolve().parents[1] / "data" / "historical_mfe.jsonl"
MIN_SAMPLES = 30


def record_mfe(
    *,
    symbol: str,
    strategy_version: str,
    mfe_pct: float,
    horizon_min: int = 60,
    source: str = "paper",
) -> None:
    row = {
        "symbol": symbol.upper(),
        "strategy_version": strategy_version,
        "mfe_pct": float(mfe_pct),
        "horizon_min": int(horizon_min),
        "source": source,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    with _LOCK:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def _load_rows() -> list[dict]:
    if not _PATH.exists():
        return []
    out: list[dict] = []
    with _PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def lookup_mfe(
    *,
    strategy_version: str | None = None,
    horizon_min: int = 60,
) -> tuple[float | None, int]:
    """Median MFE% and sample size for TargetModel.

    Prefers the same strategy_version; falls back to all rows for that horizon
    when the strategy-specific bucket is still thin.
    """
    rows = _load_rows()
    same = [
        float(r["mfe_pct"])
        for r in rows
        if int(r.get("horizon_min", 60)) == horizon_min
        and (strategy_version is None or r.get("strategy_version") == strategy_version)
        and r.get("mfe_pct") is not None
    ]
    if len(same) >= MIN_SAMPLES:
        return float(statistics.median(same)), len(same)
    any_h = [
        float(r["mfe_pct"])
        for r in rows
        if int(r.get("horizon_min", 60)) == horizon_min and r.get("mfe_pct") is not None
    ]
    if not any_h:
        return None, 0
    return float(statistics.median(any_h)), len(any_h)


def ensure_seeded_from_aftermath(path: Path | None = None) -> int:
    """Import MFE_60m from F2 aftermath once. No-op when corpus already ≥ MIN_SAMPLES."""
    if sample_counts()["total"] >= MIN_SAMPLES:
        return 0
    src = path or Path(__file__).resolve().parents[1] / "data" / "buy_aftermath_f2.json"
    if not src.exists():
        return 0
    payload = json.loads(src.read_text())
    outcomes = (
        payload.get("outcomes_by_cohort", {}).get("deduped_5m_rth")
        or payload.get("outcomes_by_cohort", {}).get("confluence_0_2_deduped_5m_rth")
        or []
    )
    n = 0
    for o in outcomes:
        mfe = (o.get("mfe") or {}).get("60m")
        if mfe is None:
            continue
        record_mfe(
            symbol=str(o.get("symbol") or "UNK"),
            strategy_version=str(o.get("strategy_version") or "unknown"),
            mfe_pct=float(mfe) * 100.0,  # aftermath stores fraction
            horizon_min=60,
            source="f2_aftermath",
        )
        n += 1
    return n


def sync_from_paper_journal(*, limit: int = 200) -> int:
    """Append MFE% from closed Paper journal rows (forward accumulation)."""
    try:
        from trading.ledger import PositionLedger

        ledger = PositionLedger()
        closed = ledger.list_closed_journal(limit=limit)
    except Exception:
        return 0
    existing = {
        (r.get("symbol"), r.get("strategy_version"), round(float(r["mfe_pct"]), 4))
        for r in _load_rows()
        if r.get("source") == "paper_journal" and r.get("mfe_pct") is not None
    }
    n = 0
    for row in closed:
        if row.mfe_pct is None:
            continue
        key = (row.symbol.upper(), row.strategy_version, round(float(row.mfe_pct), 4))
        if key in existing:
            continue
        record_mfe(
            symbol=row.symbol,
            strategy_version=row.strategy_version,
            mfe_pct=float(row.mfe_pct),
            horizon_min=60,
            source="paper_journal",
        )
        existing.add(key)
        n += 1
    return n


def sample_counts() -> dict:
    rows = _load_rows()
    by_h: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for r in rows:
        key = str(r.get("horizon_min", 60))
        by_h[key] = by_h.get(key, 0) + 1
        src = str(r.get("source") or "unknown")
        by_src[src] = by_src.get(src, 0) + 1
    return {
        "total": len(rows),
        **{f"h{k}": v for k, v in by_h.items()},
        "by_source": by_src,
    }
