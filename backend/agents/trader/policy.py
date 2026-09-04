"""Five desk steps (0/25/50/75/100) → Structure / Setup floors.

Derived from ``EntryThresholds`` — candidate gates stay Medium; the slider
only changes buy-confirmation knobs.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.entry_policy import (
    ENTRY_LEVEL_LABELS,
    EntryThresholds,
    clamp_aggressiveness,
    get_entry_thresholds,
)


@dataclass(frozen=True)
class TraderGatePolicy:
    aggressiveness: int
    label: str
    """Structure: D1 must be labelled uptrend."""
    require_uptrend: bool
    """Structure: D1 range is acceptable."""
    allow_range: bool
    """Structure: require EMA50>EMA200."""
    require_ema_stack: bool
    """Setup: reject when RSI >= this."""
    rsi_overbought: float
    """Setup: reject when |close/SMA20 - 1| above this and price extended up."""
    chase_ext_frac: float
    """Setup: 'near SMA20' band."""
    near_sma_frac: float
    """Setup: allow a shallow print below SMA20."""
    allow_below_sma: bool


def trader_gates_for(th: EntryThresholds | None = None) -> TraderGatePolicy:
    th = th or get_entry_thresholds()
    a = clamp_aggressiveness(th.aggressiveness)
    return TraderGatePolicy(
        aggressiveness=a,
        label=ENTRY_LEVEL_LABELS.get(a, "strong"),
        require_uptrend=th.require_uptrend,
        allow_range=th.allow_range,
        require_ema_stack=th.require_ema_stack,
        rsi_overbought=th.rsi_overbought,
        chase_ext_frac=th.chase_ext_frac,
        near_sma_frac=th.near_sma_frac,
        allow_below_sma=th.allow_below_sma,
    )


def structure_ok(
    *,
    structure: object,
    ema_ok: bool,
    policy: TraderGatePolicy | None = None,
) -> tuple[bool, list[str]]:
    policy = policy or trader_gates_for()
    reasons: list[str] = [f"policy={policy.label}", f"a={policy.aggressiveness}"]
    if structure == "downtrend":
        return False, [*reasons, "D1 downtrend"]

    if policy.require_ema_stack and not ema_ok:
        return False, [*reasons, "EMA stack not bullish"]

    if policy.require_uptrend:
        if structure != "uptrend":
            return False, [*reasons, f"D1 structure={structure}", "need uptrend"]
        return True, [*reasons, "D1 uptrend"]

    if structure == "uptrend":
        return True, [*reasons, "D1 uptrend"]
    if policy.allow_range and structure == "range":
        return True, [*reasons, "D1 range allowed"]
    # Soft/weak without EMA: any non-downtrend label.
    if not policy.require_ema_stack and structure != "downtrend":
        return True, [*reasons, f"D1 structure={structure}"]
    return False, [*reasons, f"D1 structure={structure}"]
