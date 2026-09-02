"""Five desk steps (0/25/50/75/100) → Structure / Setup floors.

Matches Settings «Сильно → Слабо». Risk, liquidity, RTH, earnings and news
are unchanged. Floors are slightly looser than the first trader-desk cut so
WAIT/BUY ideas can surface without disarming hard capital gates.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.entry_policy import (
    ENTRY_LEVELS,
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


# Explicit five rungs — do not collapse adjacent steps.
_GATES: dict[int, TraderGatePolicy] = {
    0: TraderGatePolicy(
        aggressiveness=0,
        label="strong",
        require_uptrend=True,
        allow_range=False,
        require_ema_stack=True,
        rsi_overbought=70.0,
        chase_ext_frac=0.035,
        near_sma_frac=0.025,
        allow_below_sma=False,
    ),
    25: TraderGatePolicy(
        aggressiveness=25,
        label="firmer",
        require_uptrend=True,
        allow_range=False,
        require_ema_stack=True,
        rsi_overbought=72.0,
        chase_ext_frac=0.040,
        near_sma_frac=0.028,
        allow_below_sma=True,
    ),
    50: TraderGatePolicy(
        aggressiveness=50,
        label="medium",
        require_uptrend=False,
        allow_range=True,
        require_ema_stack=True,
        rsi_overbought=74.0,
        chase_ext_frac=0.048,
        near_sma_frac=0.032,
        allow_below_sma=True,
    ),
    75: TraderGatePolicy(
        aggressiveness=75,
        label="softer",
        require_uptrend=False,
        allow_range=True,
        require_ema_stack=False,
        rsi_overbought=77.0,
        chase_ext_frac=0.055,
        near_sma_frac=0.038,
        allow_below_sma=True,
    ),
    100: TraderGatePolicy(
        aggressiveness=100,
        label="weak",
        require_uptrend=False,
        allow_range=True,
        require_ema_stack=False,
        rsi_overbought=80.0,
        chase_ext_frac=0.070,
        near_sma_frac=0.045,
        allow_below_sma=True,
    ),
}

assert set(_GATES) == set(ENTRY_LEVELS), "trader gates must cover every desk step"


def trader_gates_for(th: EntryThresholds | None = None) -> TraderGatePolicy:
    th = th or get_entry_thresholds()
    a = clamp_aggressiveness(th.aggressiveness)
    return _GATES[a]


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
