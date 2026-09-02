#!/usr/bin/env python3
"""F2: post-entry aftermath for historical Paper BUY proposals.

Reads TradeCandidateProposed (+ optional OpportunityApproved fills) from the
local journal, re-fetches 5m OHLCV from Alpaca, and computes horizon returns,
MFE/MAE, stop-before-target, and a deterministic failure class.

Does not change strategy behaviour. Output: stdout summary + JSON under data/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import get_settings
from core.enums import Timeframe
from core.schemas import Bar
from market_data.providers.alpaca import AlpacaMarketData

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

HORIZONS_MIN = (5, 15, 30, 60, 120)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "traido_journal.db"
DEFAULT_OUT = ROOT / "data" / "buy_aftermath_f2.json"


@dataclass
class Signal:
    symbol: str
    ts: datetime
    entry: float
    stop: float
    target: float
    strategy_version: str
    technical_score: int | None
    confidence: float | None
    source: str  # proposed | executed
    event_id: str | None = None


@dataclass
class Outcome:
    symbol: str
    ts: str
    entry: float
    stop: float
    target: float
    strategy_version: str
    source: str
    technical_score: int | None
    returns: dict[str, float | None]
    mfe: dict[str, float | None]
    mae: dict[str, float | None]
    time_to_mfe_min: dict[str, float | None]
    time_to_mae_min: dict[str, float | None]
    stop_before_target: bool | None
    target_before_stop: bool | None
    immediate_adverse: bool | None
    classification: str
    bars_used: int


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def load_signals(db_path: Path) -> list[Signal]:
    con = sqlite3.connect(db_path)
    out: list[Signal] = []
    for event_id, created, payload in con.execute(
        "SELECT id, created_at, payload FROM audit_events "
        "WHERE event_type = 'TradeCandidateProposed' ORDER BY id"
    ):
        p = json.loads(payload)
        try:
            entry = float(p["entry"])
            stop = float(p["stop"])
            target = float(p["target"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            Signal(
                symbol=str(p["symbol"]).upper(),
                ts=_parse_ts(created),
                entry=entry,
                stop=stop,
                target=target,
                strategy_version=str(p.get("strategy_version") or "?"),
                technical_score=p.get("technical_score"),
                confidence=p.get("confidence"),
                source="proposed",
                event_id=str(event_id),
            )
        )

    # Executed path: join OpportunityApproved → opportunity payload candidate.
    opp_by_id: dict[str, dict] = {}
    for (payload,) in con.execute("SELECT payload FROM opportunities"):
        p = json.loads(payload)
        oid = str(p.get("id") or "")
        if oid:
            opp_by_id[oid] = p

    for event_id, created, payload in con.execute(
        "SELECT id, created_at, payload FROM audit_events "
        "WHERE event_type = 'OpportunityApproved' ORDER BY id"
    ):
        p = json.loads(payload)
        oid = str(p.get("opportunity_id") or "")
        opp = opp_by_id.get(oid)
        if not opp:
            continue
        cand = opp.get("candidate") or {}
        fill = p.get("entry_fill")
        try:
            entry = float(fill) if fill not in (None, "None") else float(cand["entry"])
            stop = float(cand["stop"])
            target = float(cand["target"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            Signal(
                symbol=str(cand.get("symbol") or opp.get("symbol") or "?").upper(),
                ts=_parse_ts(created),
                entry=entry,
                stop=stop,
                target=target,
                strategy_version=str(cand.get("strategy_version") or "?"),
                technical_score=cand.get("technical_score"),
                confidence=cand.get("confidence"),
                source="executed",
                event_id=str(event_id),
            )
        )
    con.close()
    return out


def dedupe_first_per_symbol_day(signals: list[Signal]) -> list[Signal]:
    seen: set[tuple[str, str]] = set()
    out: list[Signal] = []
    for s in sorted(signals, key=lambda x: x.ts):
        key = (s.symbol, s.ts.date().isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def dedupe_5m_bucket(signals: list[Signal]) -> list[Signal]:
    seen: set[tuple[str, int]] = set()
    out: list[Signal] = []
    for s in sorted(signals, key=lambda x: x.ts):
        bucket = int(s.ts.timestamp() // 300)
        key = (s.symbol, bucket)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(statistics.median(xs))


def _pct(n: int, d: int) -> float | None:
    if d <= 0:
        return None
    return 100.0 * n / d


def classify(outcome: Outcome) -> str:
    """Deterministic post-entry class. Order matters."""
    r5 = outcome.returns.get("5m")
    r15 = outcome.returns.get("15m")
    r60 = outcome.returns.get("60m")
    r120 = outcome.returns.get("120m")
    mae5 = outcome.mae.get("5m")
    mfe60 = outcome.mfe.get("60m")
    mfe120 = outcome.mfe.get("120m")

    if all(v is None for v in outcome.returns.values()):
        return "INSUFFICIENT_DATA"

    planned_risk = abs(outcome.entry - outcome.stop) / outcome.entry if outcome.entry else None
    planned_reward = abs(outcome.target - outcome.entry) / outcome.entry if outcome.entry else None

    # Direction at longest available horizon.
    long_r = next((x for x in (r120, r60, r15, r5) if x is not None), None)
    if (
        long_r is not None
        and long_r <= -0.002
        and (r60 is None or r60 <= 0)
        and (r120 is None or r120 <= 0)
    ):
        if (r5 is not None and r5 < 0) and (r15 is not None and r15 < 0):
            return "DIRECTION_WRONG"

    if outcome.stop_before_target is True and long_r is not None and long_r > 0:
        return "STOP_TOO_TIGHT"

    if (
        outcome.stop_before_target is True
        and planned_risk is not None
        and mae5 is not None
        and abs(mae5) >= 0.9 * planned_risk
        and (mfe60 is not None and mfe60 > planned_risk)
    ):
        return "STOP_TOO_TIGHT"

    if (
        planned_reward is not None
        and mfe120 is not None
        and mfe120 < 0.4 * planned_reward
        and long_r is not None
        and long_r < planned_reward * 0.3
    ):
        return "TARGET_TOO_AMBITIOUS"

    immediate_red = (r5 is not None and r5 < 0) or (mae5 is not None and mae5 < -0.0005)
    later_green = (r60 is not None and r60 > 0) or (r120 is not None and r120 > 0)
    if immediate_red and later_green:
        return "ENTRY_TOO_LATE"

    if (
        r5 is not None
        and r5 > 0.001
        and r60 is not None
        and r60 < -0.001
        and (mfe60 is not None and mfe60 > 0)
    ):
        return "ENTRY_TOO_EARLY"

    if long_r is not None and long_r < -0.001:
        return "DIRECTION_WRONG"

    if long_r is not None and long_r >= 0:
        return "UNKNOWN"  # profitable / unclear failure mode

    return "INSUFFICIENT_DATA"


def is_rth(ts: datetime) -> bool:
    local = ts.astimezone(ET)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return RTH_OPEN <= t < RTH_CLOSE


def evaluate(signal: Signal, bars: list[Bar]) -> Outcome:
    entry = signal.entry
    # Anchor at the first bar at/after the signal so premarket proposals that
    # only trade once RTH opens are not scored against empty 5–120m windows.
    future = [b for b in bars if b.ts >= signal.ts]
    if not future:
        future = []
        t0 = signal.ts
        after: list[Bar] = []
    else:
        t0 = future[0].ts
        after = future

    returns: dict[str, float | None] = {}
    mfe: dict[str, float | None] = {}
    mae: dict[str, float | None] = {}
    t_mfe: dict[str, float | None] = {}
    t_mae: dict[str, float | None] = {}

    for h in HORIZONS_MIN:
        window = [b for b in after if b.ts <= t0 + timedelta(minutes=h)]
        if not window:
            returns[f"{h}m"] = None
            mfe[f"{h}m"] = None
            mae[f"{h}m"] = None
            t_mfe[f"{h}m"] = None
            t_mae[f"{h}m"] = None
            continue
        last = window[-1]
        returns[f"{h}m"] = float(last.close) / entry - 1.0
        best_i = max(range(len(window)), key=lambda i: float(window[i].high))
        worst_i = min(range(len(window)), key=lambda i: float(window[i].low))
        mfe[f"{h}m"] = float(window[best_i].high) / entry - 1.0
        mae[f"{h}m"] = float(window[worst_i].low) / entry - 1.0
        t_mfe[f"{h}m"] = (window[best_i].ts - t0).total_seconds() / 60.0
        t_mae[f"{h}m"] = (window[worst_i].ts - t0).total_seconds() / 60.0

    stop_before_target: bool | None = None
    target_before_stop: bool | None = None
    if after:
        hit_stop = False
        hit_target = False
        for b in after:
            if float(b.low) <= signal.stop:
                hit_stop = True
            if float(b.high) >= signal.target:
                hit_target = True
            if hit_stop and not hit_target:
                stop_before_target = True
                target_before_stop = False
                break
            if hit_target and not hit_stop:
                target_before_stop = True
                stop_before_target = False
                break
            if hit_stop and hit_target:
                # Same bar touches both — ambiguous; prefer adverse for longs.
                stop_before_target = True
                target_before_stop = False
                break
        if stop_before_target is None:
            stop_before_target = False
            target_before_stop = False

    immediate_adverse = None
    if mae.get("5m") is not None:
        immediate_adverse = mae["5m"] < -0.0005

    outcome = Outcome(
        symbol=signal.symbol,
        ts=signal.ts.isoformat(),
        entry=entry,
        stop=signal.stop,
        target=signal.target,
        strategy_version=signal.strategy_version,
        source=signal.source,
        technical_score=signal.technical_score,
        returns=returns,
        mfe=mfe,
        mae=mae,
        time_to_mfe_min=t_mfe,
        time_to_mae_min=t_mae,
        stop_before_target=stop_before_target,
        target_before_stop=target_before_stop,
        immediate_adverse=immediate_adverse,
        classification="INSUFFICIENT_DATA",
        bars_used=len(after),
    )
    outcome.classification = classify(outcome)
    return outcome


def summarize(outcomes: list[Outcome], label: str) -> dict:
    usable = [o for o in outcomes if o.classification != "INSUFFICIENT_DATA"]
    thin = [o for o in outcomes if o.classification == "INSUFFICIENT_DATA"]

    medians: dict[str, float | None] = {}
    for h in HORIZONS_MIN:
        key = f"{h}m"
        vals = [o.returns[key] for o in usable if o.returns.get(key) is not None]
        medians[key] = _median(vals)  # type: ignore[arg-type]

    mfe_med: dict[str, float | None] = {}
    mae_med: dict[str, float | None] = {}
    for h in HORIZONS_MIN:
        key = f"{h}m"
        mfe_med[key] = _median([o.mfe[key] for o in usable if o.mfe.get(key) is not None])  # type: ignore[arg-type]
        mae_med[key] = _median([o.mae[key] for o in usable if o.mae.get(key) is not None])  # type: ignore[arg-type]

    with_5 = [o for o in usable if o.returns.get("5m") is not None]
    immediate_adverse_n = sum(1 for o in with_5 if o.immediate_adverse)
    stop_first = [o for o in usable if o.stop_before_target is not None]
    stop_before_n = sum(1 for o in stop_first if o.stop_before_target)
    dir_120 = [o for o in usable if o.returns.get("120m") is not None]
    dir_correct_120 = sum(1 for o in dir_120 if (o.returns["120m"] or 0) > 0)
    dir_60 = [o for o in usable if o.returns.get("60m") is not None]
    dir_correct_60 = sum(1 for o in dir_60 if (o.returns["60m"] or 0) > 0)

    classes = Counter(o.classification for o in outcomes)

    interpretation = interpret(
        medians, immediate_adverse_n, len(with_5), dir_correct_120, len(dir_120), classes
    )

    return {
        "label": label,
        "samples_total": len(outcomes),
        "samples_usable": len(usable),
        "samples_insufficient": len(thin),
        "median_returns": medians,
        "median_mfe": mfe_med,
        "median_mae": mae_med,
        "immediate_adverse_rate_pct": _pct(immediate_adverse_n, len(with_5)),
        "stop_before_target_rate_pct": _pct(stop_before_n, len(stop_first)),
        "direction_correct_60m_pct": _pct(dir_correct_60, len(dir_60)),
        "direction_correct_120m_pct": _pct(dir_correct_120, len(dir_120)),
        "classification_counts": dict(classes),
        "interpretation": interpretation,
    }


def interpret(
    medians: dict[str, float | None],
    immediate_adverse_n: int,
    with_5_n: int,
    dir_correct_120: int,
    dir_120_n: int,
    classes: Counter,
) -> str:
    r5 = medians.get("5m")
    r60 = medians.get("60m")
    r120 = medians.get("120m")
    adverse_rate = (immediate_adverse_n / with_5_n) if with_5_n else 0.0
    dir_rate = (dir_correct_120 / dir_120_n) if dir_120_n else 0.0

    late = classes.get("ENTRY_TOO_LATE", 0)
    wrong = classes.get("DIRECTION_WRONG", 0)
    stop_tight = classes.get("STOP_TOO_TIGHT", 0)
    ambitious = classes.get("TARGET_TOO_AMBITIOUS", 0)
    usable_classes = late + wrong + stop_tight + ambitious

    if (
        r5 is not None
        and r5 < 0
        and r120 is not None
        and r120 > 0
        and adverse_rate >= 0.55
        and dir_rate >= 0.55
    ):
        return (
            "DIRECTION MAY BE VALID; ENTRY TIMING IS SYSTEMATICALLY LATE "
            f"(median 5m={r5:.2%}, 120m={r120:.2%}, immediate adverse={adverse_rate:.0%}, "
            f"dir@120m={dir_rate:.0%}; class late={late}, wrong={wrong}, stop_tight={stop_tight})."
        )
    if r60 is not None and r60 < 0 and r120 is not None and r120 < 0 and dir_rate < 0.45:
        return (
            "DIRECTION MODEL IS POOR "
            f"(median 60m={r60:.2%}, 120m={r120:.2%}, dir@120m={dir_rate:.0%}; "
            f"class wrong={wrong}, late={late})."
        )
    if (
        r5 is not None
        and r5 < 0
        and adverse_rate >= 0.55
        and r60 is not None
        and r60 > 0
        and dir_rate >= 0.5
    ):
        return (
            "DIRECTION MAY BE VALID; ENTRY TIMING IS SYSTEMATICALLY LATE "
            f"(median 5m={r5:.2%}, 60m={r60:.2%}, adverse={adverse_rate:.0%}, "
            f"dir@120m={dir_rate:.0%}; late={late}, wrong={wrong})."
        )
    if ambitious >= max(late, wrong, stop_tight) and ambitious > 0 and usable_classes > 0:
        return (
            "TARGET TOO AMBITIOUS RELATIVE TO REALIZED MFE "
            f"(ambitious={ambitious}, late={late}, wrong={wrong}; "
            f"median 5m={None if r5 is None else f'{r5:.2%}'}, "
            f"120m={None if r120 is None else f'{r120:.2%}'}, dir@120m={dir_rate:.0%})."
        )
    if stop_tight >= max(late, wrong) and stop_tight > 0:
        return (
            "STOP PLACEMENT DOMINATES FAILURES "
            f"(stop_tight={stop_tight}, late={late}, wrong={wrong}; "
            f"median 5m={None if r5 is None else f'{r5:.2%}'}, "
            f"120m={None if r120 is None else f'{r120:.2%}'})."
        )
    if late > wrong and r5 is not None and r5 < 0:
        return (
            "ENTRY TIMING LIKELY PRIMARY ISSUE "
            f"(late={late} > wrong={wrong}; median 5m={r5:.2%}, "
            f"120m={None if r120 is None else f'{r120:.2%}'})."
        )
    if wrong > late and (r60 is None or r60 < 0 or dir_rate < 0.5):
        return (
            "DIRECTION LIKELY PRIMARY ISSUE "
            f"(wrong={wrong} > late={late}; median 60m={None if r60 is None else f'{r60:.2%}'}, "
            f"120m={None if r120 is None else f'{r120:.2%}'}, dir@120m={dir_rate:.0%})."
        )
    if r5 is not None and r5 >= 0 and r60 is not None and r60 >= 0 and dir_rate >= 0.55:
        return (
            "NO SYSTEMATIC IMMEDIATE LOSS ON THIS COHORT — "
            f"median path is non-negative (5m={r5:.2%}, 60m={r60:.2%}, "
            f"dir@120m={dir_rate:.0%}, adverse={adverse_rate:.0%}). "
            "Operator pain may be concentrated in executed fills / costs / targets."
        )
    return (
        "MIXED / INCONCLUSIVE — inspect classification_counts and medians; "
        f"late={late}, wrong={wrong}, stop_tight={stop_tight}, ambitious={ambitious}, "
        f"adverse={adverse_rate:.0%}, dir@120m={dir_rate:.0%}."
    )


async def fetch_bars(
    md: AlpacaMarketData,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    sem = asyncio.Semaphore(4)

    async def one(sym: str) -> None:
        async with sem:
            try:
                bars = await md.get_bars(sym, Timeframe.M5, start, end)
                out[sym] = bars
                print(f"  bars {sym}: {len(bars)}", flush=True)
            except Exception as exc:
                print(f"  bars {sym}: FAILED {exc}", flush=True)
                out[sym] = []

    await asyncio.gather(*(one(s) for s in symbols))
    return out


def fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:+.2f}%"


def print_report(summary: dict) -> None:
    print()
    print("=" * 64)
    print(f"BUY SIGNAL AFTERMATH — {summary['label']}")
    print("=" * 64)
    print(
        f"Samples        {summary['samples_usable']} usable / {summary['samples_total']} total "
        f"({summary['samples_insufficient']} insufficient)"
    )
    print()
    print("Median returns:")
    for h in HORIZONS_MIN:
        print(f"  {h:>3}m   {fmt_pct(summary['median_returns'].get(f'{h}m'))}")
    print()
    print("Median MFE / MAE:")
    for h in HORIZONS_MIN:
        print(
            f"  {h:>3}m   MFE {fmt_pct(summary['median_mfe'].get(f'{h}m'))}   "
            f"MAE {fmt_pct(summary['median_mae'].get(f'{h}m'))}"
        )
    print()
    print(f"Immediate adverse rate:   {summary['immediate_adverse_rate_pct']}")
    print(f"Stop-before-target:       {summary['stop_before_target_rate_pct']}")
    print(f"Direction correct @60m:   {summary['direction_correct_60m_pct']}")
    print(f"Direction correct @120m:  {summary['direction_correct_120m_pct']}")
    print()
    print("Classifications:")
    for k, v in sorted(summary["classification_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:24} {v}")
    print()
    print("Interpretation:")
    print(f"  {summary['interpretation']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-symbols", type=int, default=0, help="0 = all")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise SystemExit("ALPACA_API_KEY / ALPACA_API_SECRET required")

    all_signals = load_signals(args.db)
    proposed = [s for s in all_signals if s.source == "proposed"]
    executed = [s for s in all_signals if s.source == "executed"]
    print(f"Loaded proposed={len(proposed)} executed={len(executed)}")

    proposed_rth = [s for s in proposed if is_rth(s.ts)]
    executed_rth = [s for s in executed if is_rth(s.ts)]
    cohorts = {
        "all_proposed": proposed,
        "proposed_rth": proposed_rth,
        "deduped_5m_rth": dedupe_5m_bucket(proposed_rth),
        "first_per_symbol_day_rth": dedupe_first_per_symbol_day(proposed_rth),
        "executed": executed,
        "executed_rth": executed_rth,
        "confluence_0_2_deduped_5m_rth": dedupe_5m_bucket(
            [s for s in proposed_rth if s.strategy_version.startswith("strategy_confluence@0.2")]
        ),
    }
    for name, sigs in cohorts.items():
        print(f"  cohort {name}: {len(sigs)}")

    # Cover all cohorts with one bar pull.
    needed = {s.symbol for sigs in cohorts.values() for s in sigs}
    if args.max_symbols:
        needed = set(sorted(needed)[: args.max_symbols])
        cohorts = {k: [s for s in v if s.symbol in needed] for k, v in cohorts.items()}

    if not proposed:
        raise SystemExit("no TradeCandidateProposed rows")

    start = min(s.ts for s in proposed) - timedelta(hours=1)
    end = max(s.ts for s in proposed) + timedelta(hours=3)
    print(f"Fetching 5m bars {start.isoformat()} → {end.isoformat()} for {len(needed)} symbols")

    md = AlpacaMarketData(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        base_url=settings.alpaca_data_base_url,
    )
    bars_by_sym = await fetch_bars(md, sorted(needed), start, end)

    payload: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "bar_window": {"start": start.isoformat(), "end": end.isoformat()},
        "horizons_min": list(HORIZONS_MIN),
        "notes": [
            "1m bars unsupported; 5m is the finest resolution.",
            "Returns use signal entry (or fill for executed) vs later closes.",
            "MFE/MAE from highs/lows in each horizon window.",
            "Deduped cohorts reduce rescans of the same ticker.",
        ],
        "summaries": {},
        "outcomes_by_cohort": {},
    }

    for name, sigs in cohorts.items():
        outcomes = [evaluate(s, bars_by_sym.get(s.symbol, [])) for s in sigs]
        summary = summarize(outcomes, name)
        payload["summaries"][name] = summary
        payload["outcomes_by_cohort"][name] = [asdict(o) for o in outcomes]
        print_report(summary)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
