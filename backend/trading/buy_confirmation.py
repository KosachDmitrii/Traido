"""Final BUY confirmation — slider-dependent, never candidate discovery.

``candidate_policy`` (scanner, setup, WAIT, zone, hard floors) is fixed.
This module applies only after ``BUY_READY_CANDIDATE``. The operator slider
``buy_confirmation_strictness`` (legacy: ``aggressiveness``) relaxes soft
confirms: momentum, volume, VWAP, arrival, and a few setup/entry points.
Hard risk/data/execution gates are not represented here and cannot be relaxed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.enums import AdmissionDecision
from trading.zone_arrival import ZoneArrivalFacts

# Paper/production candidate floors — independent of the slider.
CANDIDATE_SETUP_FLOOR = 55
CANDIDATE_ENTRY_FLOOR = 50
BASE_RR_FLOOR = 1.45
# Absolute quality floor for BUY_READY: candidate floor plus the weakest
# confirmation tolerance. Below this, Weak cannot mint a potential buy.
MAX_SETUP_TOLERANCE = 3
MAX_ENTRY_TOLERANCE = 3
BUY_READY_SETUP_FLOOR = CANDIDATE_SETUP_FLOOR - MAX_SETUP_TOLERANCE  # 52
BUY_READY_ENTRY_FLOOR = CANDIDATE_ENTRY_FLOOR - MAX_ENTRY_TOLERANCE  # 47

COMPENSATION_MIN_RR = 2.0
MAX_SETUP_DEFICIT = 3

# Materially negative momentum stays a hard/NO_TRADE veto at every level.
MATERIAL_NEGATIVE_MOMENTUM_PCT = -0.40
# Heavy sell / distribution — confirmation cannot waive this.
HEAVY_SELL_VOLUME_RATIO = 1.60

MOMENTUM_CONFIRMATION_MISSING = "MOMENTUM_CONFIRMATION_MISSING"
VOLUME_CONFIRMATION_MISSING = "VOLUME_CONFIRMATION_MISSING"
VWAP_CONFIRMATION_MISSING = "VWAP_CONFIRMATION_MISSING"
SETUP_CONFIRMATION_BELOW_FLOOR = "SETUP_CONFIRMATION_BELOW_FLOOR"
ENTRY_CONFIRMATION_BELOW_FLOOR = "ENTRY_CONFIRMATION_BELOW_FLOOR"
EFFECTIVE_RR_TOO_LOW = "EFFECTIVE_RR_TOO_LOW"
ARRIVAL_CONFIRMATION_MISSING = "ARRIVAL_CONFIRMATION_MISSING"
BUY_READY_CANDIDATE = "BUY_READY_CANDIDATE"
BUY_CONFIRMATION_RELAXED = "BUY_CONFIRMATION_RELAXED"
NOT_BUY_READY = "NOT_BUY_READY"

CONFIRMATION_REJECTION_CODES = frozenset(
    {
        MOMENTUM_CONFIRMATION_MISSING,
        VOLUME_CONFIRMATION_MISSING,
        VWAP_CONFIRMATION_MISSING,
        SETUP_CONFIRMATION_BELOW_FLOOR,
        ENTRY_CONFIRMATION_BELOW_FLOOR,
        EFFECTIVE_RR_TOO_LOW,
        ARRIVAL_CONFIRMATION_MISSING,
        "ARRIVAL_TYPE_SELL_OFF",
    }
)


class MomentumMode(StrEnum):
    STRONG = "strong"
    CONFIRMED = "confirmed"
    MODERATE = "moderate"
    WEAK_FLAT = "weak_flat"
    OPTIONAL = "optional"


class VolumeMode(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    SOFT = "soft"


class VwapMode(StrEnum):
    REQUIRED = "required"
    REQUIRED_UNLESS_STRUCTURE = "required_unless_structure"
    SOFT = "soft"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class BuyConfirmationPolicy:
    """Soft-confirm knobs for one desk step. Candidate fields live elsewhere."""

    strictness: int
    label: str
    setup_tolerance: int
    entry_tolerance: int
    min_effective_rr: float
    momentum_mode: MomentumMode
    volume_mode: VolumeMode
    vwap_mode: VwapMode
    momentum_min_pct: float
    require_momentum_flip: bool
    require_vwap_hold: bool
    require_vol_digest: bool
    min_zone_arrival_quality: int
    allow_fast_pullback: bool
    allow_sell_off_arrival: bool
    min_sell_off_arrival_quality: int
    min_fast_pullback_arrival_quality: int
    weak_setup_min_rr: float
    vwap_hold_min_pct: float
    vwap_anchor_hold_frac: float
    pullback_vol_digest_max: float

    @property
    def confirm_setup_floor(self) -> int:
        return CANDIDATE_SETUP_FLOOR + self.setup_tolerance

    @property
    def confirm_entry_floor(self) -> int:
        return CANDIDATE_ENTRY_FLOOR + self.entry_tolerance


BUY_CONFIRMATION_LEVELS: dict[int, BuyConfirmationPolicy] = {
    0: BuyConfirmationPolicy(
        strictness=0,
        label="strong",
        setup_tolerance=0,
        entry_tolerance=0,
        min_effective_rr=2.00,
        momentum_mode=MomentumMode.STRONG,
        volume_mode=VolumeMode.REQUIRED,
        vwap_mode=VwapMode.REQUIRED,
        momentum_min_pct=0.0,
        require_momentum_flip=True,
        require_vwap_hold=True,
        require_vol_digest=True,
        min_zone_arrival_quality=60,
        allow_fast_pullback=False,
        allow_sell_off_arrival=False,
        min_sell_off_arrival_quality=60,
        min_fast_pullback_arrival_quality=58,
        weak_setup_min_rr=2.50,
        vwap_hold_min_pct=-0.35,
        vwap_anchor_hold_frac=0.996,
        pullback_vol_digest_max=1.05,
    ),
    25: BuyConfirmationPolicy(
        strictness=25,
        label="firmer",
        setup_tolerance=-1,
        entry_tolerance=-1,
        min_effective_rr=1.90,
        momentum_mode=MomentumMode.CONFIRMED,
        volume_mode=VolumeMode.REQUIRED,
        vwap_mode=VwapMode.REQUIRED,
        momentum_min_pct=0.0,
        require_momentum_flip=False,
        require_vwap_hold=True,
        require_vol_digest=True,
        min_zone_arrival_quality=56,
        allow_fast_pullback=False,
        allow_sell_off_arrival=False,
        min_sell_off_arrival_quality=56,
        min_fast_pullback_arrival_quality=52,
        weak_setup_min_rr=2.30,
        vwap_hold_min_pct=-0.45,
        vwap_anchor_hold_frac=0.994,
        pullback_vol_digest_max=1.10,
    ),
    50: BuyConfirmationPolicy(
        strictness=50,
        label="medium",
        setup_tolerance=-2,
        entry_tolerance=-2,
        min_effective_rr=1.75,
        momentum_mode=MomentumMode.MODERATE,
        volume_mode=VolumeMode.PREFERRED,
        vwap_mode=VwapMode.REQUIRED_UNLESS_STRUCTURE,
        momentum_min_pct=-0.02,
        require_momentum_flip=False,
        require_vwap_hold=True,
        require_vol_digest=False,
        min_zone_arrival_quality=50,
        allow_fast_pullback=True,
        allow_sell_off_arrival=False,
        min_sell_off_arrival_quality=50,
        min_fast_pullback_arrival_quality=48,
        weak_setup_min_rr=2.10,
        vwap_hold_min_pct=-0.55,
        vwap_anchor_hold_frac=0.992,
        pullback_vol_digest_max=1.15,
    ),
    75: BuyConfirmationPolicy(
        strictness=75,
        label="softer",
        setup_tolerance=-3,
        entry_tolerance=-3,
        min_effective_rr=1.60,
        momentum_mode=MomentumMode.WEAK_FLAT,
        volume_mode=VolumeMode.SOFT,
        vwap_mode=VwapMode.SOFT,
        momentum_min_pct=-0.08,
        require_momentum_flip=False,
        require_vwap_hold=False,
        require_vol_digest=False,
        min_zone_arrival_quality=44,
        allow_fast_pullback=True,
        allow_sell_off_arrival=True,
        min_sell_off_arrival_quality=22,
        min_fast_pullback_arrival_quality=40,
        weak_setup_min_rr=1.80,
        vwap_hold_min_pct=-0.62,
        vwap_anchor_hold_frac=0.990,
        pullback_vol_digest_max=1.22,
    ),
    100: BuyConfirmationPolicy(
        strictness=100,
        label="weak",
        setup_tolerance=-3,
        entry_tolerance=-3,
        min_effective_rr=1.45,
        momentum_mode=MomentumMode.OPTIONAL,
        volume_mode=VolumeMode.SOFT,
        vwap_mode=VwapMode.OPTIONAL,
        momentum_min_pct=-0.15,
        require_momentum_flip=False,
        require_vwap_hold=False,
        require_vol_digest=False,
        min_zone_arrival_quality=35,
        allow_fast_pullback=True,
        allow_sell_off_arrival=True,
        min_sell_off_arrival_quality=8,
        min_fast_pullback_arrival_quality=28,
        weak_setup_min_rr=1.45,
        vwap_hold_min_pct=-0.70,
        vwap_anchor_hold_frac=0.988,
        pullback_vol_digest_max=1.28,
    ),
}


def buy_confirmation_for(strictness: int) -> BuyConfirmationPolicy:
    from trading.entry_policy import clamp_aggressiveness

    return BUY_CONFIRMATION_LEVELS[clamp_aggressiveness(strictness)]


@dataclass(frozen=True)
class BuyReadyResult:
    ready: bool
    reason_codes: list[str]
    blocked_decision: AdmissionDecision | None = None


@dataclass(frozen=True)
class ConfirmationResult:
    passed: bool
    relaxed: bool
    reason_codes: list[str]
    warnings: list[str]


def evaluate_buy_ready(
    *,
    candidate_exists: bool,
    structurally_valid: bool,
    price_in_entry_zone: bool,
    stop_valid: bool,
    target_valid: bool,
    planned_rr: float | None,
    data_fresh: bool,
    regime_allowed: bool,
    hard_veto: bool,
    setup_quality: int,
    entry_quality: int,
    thesis_bullish: bool = True,
) -> BuyReadyResult:
    """Slider-independent precondition. If False, confirmation is not applied."""
    reasons: list[str] = []
    if not candidate_exists:
        reasons.append("CANDIDATE_MISSING")
    if not thesis_bullish:
        reasons.append("THESIS_NOT_BULLISH")
    if not structurally_valid:
        reasons.append("STRUCTURAL_DAMAGE")
    if not price_in_entry_zone:
        reasons.append("ENTRY_OUTSIDE_ALLOWED_ZONE")
    if not stop_valid:
        reasons.append("INVALID_STOP")
    if not target_valid:
        reasons.append("INVALID_TARGET")
    if planned_rr is None or planned_rr < BASE_RR_FLOOR:
        reasons.append("PLANNED_RR_BELOW_BASE_FLOOR")
    if not data_fresh:
        reasons.append("DATA_BLOCKED")
    if not regime_allowed:
        reasons.append("REGIME_NOT_ALLOWED")
    if hard_veto:
        reasons.append("HARD_VETO")
    if setup_quality < BUY_READY_SETUP_FLOOR:
        reasons.append("CANDIDATE_SETUP_BELOW_FLOOR")
    if entry_quality < BUY_READY_ENTRY_FLOOR:
        reasons.append("CANDIDATE_ENTRY_BELOW_FLOOR")

    if reasons:
        blocked = AdmissionDecision.DATA_BLOCKED if not data_fresh else None
        if blocked is None and (
            not structurally_valid
            or not thesis_bullish
            or not stop_valid
            or not target_valid
            or hard_veto
        ):
            blocked = AdmissionDecision.NO_TRADE
        if blocked is None:
            blocked = AdmissionDecision.WAIT
        return BuyReadyResult(ready=False, reason_codes=reasons, blocked_decision=blocked)
    return BuyReadyResult(ready=True, reason_codes=[BUY_READY_CANDIDATE])


def _vwap_holds(
    *,
    price: float,
    distance_from_vwap_pct: float | None,
    anchor_price: float | None,
    policy: BuyConfirmationPolicy,
) -> bool | None:
    """True/False when measurable; None when VWAP facts are missing."""
    if distance_from_vwap_pct is None and anchor_price is None:
        return None
    if distance_from_vwap_pct is not None and distance_from_vwap_pct < policy.vwap_hold_min_pct:
        return False
    return not (anchor_price is not None and price < anchor_price * policy.vwap_anchor_hold_frac)


def evaluate_buy_confirmation(
    *,
    policy: BuyConfirmationPolicy,
    setup_quality: int,
    entry_quality: int,
    planned_rr: float | None,
    effective_rr: float | None,
    momentum_pct: float | None,
    pullback_vol_ratio: float | None,
    price: float,
    distance_from_vwap_pct: float | None,
    anchor_price: float | None,
    structure_valid: bool,
    paper: bool,
    arrival: ZoneArrivalFacts | None = None,
    arrival_required: bool = False,
) -> ConfirmationResult:
    """Soft confirms for a BUY_READY candidate. Never waives a hard gate."""
    reasons: list[str] = []
    warnings: list[str] = []
    relaxed = False

    if momentum_pct is not None and momentum_pct <= MATERIAL_NEGATIVE_MOMENTUM_PCT:
        reasons.append(MOMENTUM_CONFIRMATION_MISSING)
        return ConfirmationResult(
            passed=False, relaxed=False, reason_codes=reasons, warnings=warnings
        )

    if pullback_vol_ratio is not None and pullback_vol_ratio >= HEAVY_SELL_VOLUME_RATIO:
        reasons.append(VOLUME_CONFIRMATION_MISSING)
        return ConfirmationResult(
            passed=False, relaxed=False, reason_codes=reasons, warnings=warnings
        )

    setup_floor = policy.confirm_setup_floor
    entry_floor = policy.confirm_entry_floor
    setup_deficit = float(CANDIDATE_SETUP_FLOOR) - float(setup_quality)

    if setup_quality < setup_floor:
        if (
            paper
            and 0 < setup_deficit <= MAX_SETUP_DEFICIT
            and entry_quality >= entry_floor
            and planned_rr is not None
            and planned_rr >= COMPENSATION_MIN_RR
        ):
            reasons.append("SETUP_COMPENSATED")
            relaxed = True
        else:
            reasons.append(SETUP_CONFIRMATION_BELOW_FLOOR)
    elif setup_quality < CANDIDATE_SETUP_FLOOR and policy.setup_tolerance < 0:
        relaxed = True

    if entry_quality < entry_floor:
        reasons.append(ENTRY_CONFIRMATION_BELOW_FLOOR)
    elif entry_quality < CANDIDATE_ENTRY_FLOOR and policy.entry_tolerance < 0:
        relaxed = True

    rr_val = effective_rr if effective_rr is not None else planned_rr
    if rr_val is None or rr_val < policy.min_effective_rr:
        reasons.append(EFFECTIVE_RR_TOO_LOW)
        if rr_val is not None:
            reasons.append(f"INSUFFICIENT_EFFECTIVE_RR:{rr_val:.2f}<{policy.min_effective_rr:.2f}")

    mom_ok = _momentum_ok(momentum_pct, policy, structure_valid=structure_valid)
    if mom_ok is False:
        reasons.append(MOMENTUM_CONFIRMATION_MISSING)
    elif mom_ok is None and policy.momentum_mode in {
        MomentumMode.MODERATE,
        MomentumMode.WEAK_FLAT,
        MomentumMode.OPTIONAL,
    }:
        relaxed = True

    vol_ok = _volume_ok(pullback_vol_ratio, policy)
    if vol_ok is False and policy.volume_mode is VolumeMode.REQUIRED:
        reasons.append(VOLUME_CONFIRMATION_MISSING)
    elif (
        vol_ok is not True
        and policy.volume_mode is VolumeMode.PREFERRED
        or vol_ok is not True
        and policy.volume_mode is VolumeMode.SOFT
    ):
        warnings.append(VOLUME_CONFIRMATION_MISSING)
        relaxed = True

    vwap_ok = _vwap_holds(
        price=price,
        distance_from_vwap_pct=distance_from_vwap_pct,
        anchor_price=anchor_price,
        policy=policy,
    )
    if policy.vwap_mode is VwapMode.REQUIRED:
        if vwap_ok is not True:
            reasons.append(VWAP_CONFIRMATION_MISSING)
    elif policy.vwap_mode is VwapMode.REQUIRED_UNLESS_STRUCTURE:
        if vwap_ok is True:
            pass
        elif structure_valid:
            warnings.append(VWAP_CONFIRMATION_MISSING)
            relaxed = True
        else:
            reasons.append(VWAP_CONFIRMATION_MISSING)
    elif policy.vwap_mode is VwapMode.SOFT:
        if vwap_ok is not True:
            warnings.append(VWAP_CONFIRMATION_MISSING)
            relaxed = True
    elif policy.vwap_mode is VwapMode.OPTIONAL and vwap_ok is False:
        warnings.append(VWAP_CONFIRMATION_MISSING)
        relaxed = True

    if arrival_required:
        if arrival is None:
            reasons.append(ARRIVAL_CONFIRMATION_MISSING)
        else:
            from trading.arrival_admission import evaluate_soft_arrival

            soft = evaluate_soft_arrival(
                arrival,
                min_zone_arrival_quality=policy.min_zone_arrival_quality,
                allow_fast_pullback=policy.allow_fast_pullback,
                allow_sell_off_arrival=policy.allow_sell_off_arrival,
                min_sell_off_arrival_quality=policy.min_sell_off_arrival_quality,
                min_fast_pullback_arrival_quality=policy.min_fast_pullback_arrival_quality,
            )
            if soft.blocked:
                reasons.extend(soft.reason_codes)
                if ARRIVAL_CONFIRMATION_MISSING not in reasons:
                    reasons.append(ARRIVAL_CONFIRMATION_MISSING)
            warnings.extend(soft.warnings)

    hard_fail = any(_is_confirmation_rejection(c) for c in reasons)
    if hard_fail:
        return ConfirmationResult(
            passed=False, relaxed=False, reason_codes=reasons, warnings=warnings
        )
    if relaxed:
        reasons.append(BUY_CONFIRMATION_RELAXED)
    return ConfirmationResult(passed=True, relaxed=relaxed, reason_codes=reasons, warnings=warnings)


def _momentum_ok(
    momentum_pct: float | None,
    policy: BuyConfirmationPolicy,
    *,
    structure_valid: bool,
) -> bool | None:
    """True pass, False fail, None = optional/skipped."""
    if policy.momentum_mode is MomentumMode.STRONG:
        if momentum_pct is None:
            return False
        return momentum_pct > policy.momentum_min_pct
    if policy.momentum_mode is MomentumMode.CONFIRMED:
        if momentum_pct is None:
            return False
        return momentum_pct >= policy.momentum_min_pct
    if policy.momentum_mode is MomentumMode.MODERATE:
        if momentum_pct is None:
            return None
        return momentum_pct >= policy.momentum_min_pct
    if policy.momentum_mode is MomentumMode.WEAK_FLAT:
        if momentum_pct is None:
            return None
        if momentum_pct >= policy.momentum_min_pct and structure_valid:
            return True
        return momentum_pct >= 0.0
    # OPTIONAL
    if momentum_pct is None:
        return None
    return momentum_pct > MATERIAL_NEGATIVE_MOMENTUM_PCT


def _is_confirmation_rejection(code: str) -> bool:
    if code == "SETUP_COMPENSATED":
        return False
    if code in CONFIRMATION_REJECTION_CODES:
        return True
    return code.startswith(("INSUFFICIENT_EFFECTIVE_RR:", "ZONE_ARRIVAL_QUALITY_LOW"))


def _volume_ok(pullback_vol_ratio: float | None, policy: BuyConfirmationPolicy) -> bool | None:
    if pullback_vol_ratio is None:
        return None if policy.volume_mode is not VolumeMode.REQUIRED else False
    return pullback_vol_ratio <= policy.pullback_vol_digest_max
