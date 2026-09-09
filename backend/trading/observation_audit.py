"""Same-input desk-policy comparison and replayable Alpaca evidence. No orders."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import zlib
from datetime import UTC, datetime
from typing import Any

from agents.trader.risk_plan import MIN_RR
from agents.trader.structure import run_structure
from agents.trader.types import TraderBundle
from core.enums import Timeframe
from core.ports import AuditPort
from core.schemas import Bar, PipelineResult
from quant.engine import compute_features
from trading.buy_confirmation import BASE_RR_FLOOR
from trading.effective_rr import planned_long_rr
from trading.observation_policy import OBSERVATION_POLICY_VERSION

logger = logging.getLogger(__name__)


def compare_structure(bundle: TraderBundle) -> dict[str, Any]:
    """Both policies see the exact same feature objects; no second vendor read."""
    baseline = TraderBundle(symbol=bundle.symbol, features=bundle.features)
    observation = TraderBundle(
        symbol=bundle.symbol, features=bundle.features, observation_mode=True
    )
    old = run_structure(baseline)
    new = run_structure(observation)
    return {
        "baseline_pass": old.ok,
        "observation_pass": new.ok,
        "baseline_reasons": old.reasons,
        "requirements": observation.observation_requirements,
    }


def encode_bars(source: dict[str, list[Bar]]) -> dict[str, str]:
    raw = json.dumps(
        {tf: [b.model_dump(mode="json") for b in bars] for tf, bars in source.items()},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "zlib_base64": base64.b64encode(zlib.compress(raw)).decode(),
    }


def replay_features(evidence: dict[str, Any]) -> dict[str, Any]:
    """Verify original bytes, then independently rebuild the measured features."""
    packed = evidence["source_bars"]
    raw = zlib.decompress(base64.b64decode(packed["zlib_base64"]))
    if hashlib.sha256(raw).hexdigest() != packed["sha256"]:
        raise ValueError("EVIDENCE_HASH_MISMATCH")
    source = json.loads(raw)
    differences = []
    for tf, expected in evidence["features"].items():
        bars = [Bar.model_validate(b) for b in source[tf]]
        actual = compute_features(evidence["symbol"], Timeframe(tf), bars).model_dump(mode="json")
        for key in ("indicators", "chart_patterns", "support", "resistance"):
            if actual[key] != expected[key]:
                differences.append(f"{tf}:{key}")
        # Independent arithmetic, not a second call to the same indicator code.
        close = [float(b.close) for b in bars]
        reference: dict[str, float | None] = {"close": close[-1]}
        for period in (50, 200):
            value = None
            if len(close) >= period:
                decay = 1 - 2 / (period + 1)
                tail = close[period:]
                value = math.fsum(close[:period]) / period * decay ** len(tail)
                value += (1 - decay) * math.fsum(v * decay**i for i, v in enumerate(reversed(tail)))
            reference[f"ema_{period}"] = value
        if len(bars) >= 14:
            ranges = [
                max(
                    float(b.high - b.low),
                    abs(float(b.high) - close[i - 1]),
                    abs(float(b.low) - close[i - 1]),
                )
                if i
                else float(b.high - b.low)
                for i, b in enumerate(bars)
            ]
            reference["atr_14"] = math.fsum(ranges[-14:]) / 14
        for key, value in reference.items():
            measured = expected["indicators"].get(key)
            if value is None:
                matches = measured is None
            else:
                matches = isinstance(measured, (int, float)) and math.isclose(
                    value, measured, rel_tol=1e-9, abs_tol=1e-8
                )
            if not matches:
                differences.append(f"{tf}:independent_{key}")
    return {
        "symbol": evidence["symbol"],
        "differences": differences,
        "verified": not differences if evidence["features"] else None,
        "checked_timeframes": list(evidence["features"]),
    }


async def record_observation_evidence(
    bundle: TraderBundle, result: PipelineResult, audit: AuditPort
) -> None:
    features = {tf.value: snap.model_dump(mode="json") for tf, snap in bundle.features.items()}
    comparison = compare_structure(bundle) if Timeframe.D1 in bundle.features else None
    old_rr = planned_long_rr(*bundle.original_plan) if bundle.original_plan else None
    plan_rr = planned_long_rr(*bundle._planned) if bundle._planned else None
    summary = {
        "symbol": bundle.symbol,
        "pipeline_run_id": str(result.pipeline_run_id),
        "policy": OBSERVATION_POLICY_VERSION,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "status": result.status,
        "structure": comparison,
        "original_plan": bundle.original_plan,
        "observation_plan": bundle._planned,
        "baseline_rr": old_rr,
        "observation_rr": plan_rr,
        "baseline_rr_floor": MIN_RR,
        "observation_rr_floor": BASE_RR_FLOOR,
        "requirements": bundle.observation_requirements,
        "measurements": {
            tf: {
                "bars": len(bundle.source_bars.get(tf, [])),
                "last_bar_ts": bundle.source_bars[tf][-1].ts.isoformat()
                if bundle.source_bars.get(tf)
                else None,
                "structure": f["chart_patterns"].get("structure"),
                "ema50": f["indicators"].get("ema_50"),
                "ema200": f["indicators"].get("ema_200"),
                "close": f["indicators"].get("close"),
                "atr": f["indicators"].get("atr_14"),
                "support": f["support"],
                "resistance": f["resistance"],
            }
            for tf, f in features.items()
        },
        "errors": result.errors,
    }
    evidence = {
        **summary,
        "features": features,
        "source_bars": encode_bars(bundle.source_bars),
        "entry_decision": result.entry_decision.model_dump(mode="json")
        if result.entry_decision
        else None,
        "market": result.market.model_dump(mode="json") if result.market else None,
        "steps": [{"step": s.step.value, "ok": s.ok, "reasons": s.reasons} for s in bundle.steps],
    }
    # Recompute from captured inputs immediately; no refetch at a different price.
    verified = replay_features(evidence)
    summary["replay"] = verified
    evidence["replay"] = verified
    await audit.append(
        "ObservationPolicyEvidence",
        "trader_desk",
        evidence,
        pipeline_run_id=result.pipeline_run_id,
        entity_type="symbol",
        entity_id=bundle.symbol,
    )
    logger.info("ObservationPolicyComparison %s", json.dumps(summary, default=str, allow_nan=False))
    if verified["differences"]:
        raise ValueError("OBSERVATION_FEATURE_REPLAY_MISMATCH")
