"""
Pydantic v2 domain contracts.

RULES:
- Agents may ONLY emit models defined here (or strict subclasses).
- Invalid payload ⇒ reject + audit SCHEMA_INVALID — never coerce into an order.
- No broker credentials, SQL, or raw HTTP inside these models.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.enums import (
    AssessmentKind,
    EarningsCheck,
    EntryDecision,
    EntryWatchStatus,
    InstrumentThesis,
    IntentPurpose,
    MarketRegimeLabel,
    NewsCheck,
    OpportunityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskVerdict,
    SessionCohort,
    TargetReachabilityClass,
    Timeframe,
    TradeAction,
    TradingMode,
    UserDecision,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MoneyDecimal(StrictModel):
    """Marker docs — use Decimal fields directly on models."""


# ── Market data ──────────────────────────────────────────────────────────────


class Bar(StrictModel):
    symbol: str = Field(min_length=1, max_length=16)
    timeframe: Timeframe
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str


class Snapshot(StrictModel):
    """One symbol's cheap picture of today, from a batched read.

    The unit of Stage 1. Everything here arrives from a single multi-symbol
    request, which is the whole reason a thousand-name universe is affordable:
    the same facts fetched one symbol at a time cost a thousand round trips.

    Every field is optional because a batch answer is not a promise about each
    member of the batch — a thin name may have no quote and a halted one no
    trade. Absent stays absent; Stage 1 rejects on missing mandatory data rather
    than substituting a zero, which would read as "free" and "illiquid" exactly
    where those are the most dangerous answers.
    """

    symbol: str = Field(min_length=1, max_length=16)
    price: Decimal | None = None
    """Last trade. Not tradable on its own — Stage 1 screens, execution prices."""

    bid: Decimal | None = None
    ask: Decimal | None = None
    day_volume: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    prev_close: Decimal | None = None
    trade_ts: datetime | None = None
    quote_ts: datetime | None = None
    source: str = "unknown"

    @property
    def spread_bps(self) -> float | None:
        """Basis points of the mid, or None when the book is absent or crossed.

        A crossed or zero book returns None rather than a negative number: it is
        bad data, not a tight spread, and the difference decides whether a name
        is the most attractive on the desk or is thrown out.
        """
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return float((self.ask - self.bid) / mid * 10000)


class Quote(StrictModel):
    """Top of book at an instant. The only honest source of a real spread.

    `ts` is mandatory because a quote without a timestamp cannot be judged
    stale, and an unjudgeable quote is indistinguishable from a stale one.
    """

    symbol: str = Field(min_length=1, max_length=16)
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    ts: datetime
    source: str


class FeatureSnapshot(StrictModel):
    symbol: str
    timeframe: Timeframe
    computed_at: datetime
    indicators: dict[str, float | int | bool | str | None]
    candlestick_patterns: dict[str, bool]
    chart_patterns: dict[str, bool | str | None]
    support: list[Decimal] = Field(default_factory=list)
    resistance: list[Decimal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── Agent assessments ────────────────────────────────────────────────────────


class TechnicalAssessment(StrictModel):
    kind: AssessmentKind = AssessmentKind.TECHNICAL
    symbol: str
    trend: str
    score: int = Field(ge=0, le=100)
    rsi: float | None = None
    relative_volume: float | None = None
    breakout_confirmed: bool = False
    support_confirmed: bool = False
    ema50_above_ema200: bool | None = None
    pattern_flags: dict[str, bool] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    timeframe_summary: dict[str, str] = Field(default_factory=dict)


class NewsAssessment(StrictModel):
    kind: AssessmentKind = AssessmentKind.NEWS
    symbol: str
    sentiment: str  # positive | negative | mixed | neutral
    score: int = Field(ge=0, le=100)
    material_events: list[str] = Field(default_factory=list)
    headlines: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    status: NewsCheck = NewsCheck.NOT_CHECKED
    """Whether `sentiment` and `score` came from headlines that were read.

    Defaults to "nobody looked", so a caller that builds an assessment without
    consulting a vendor cannot accidentally present it as a cleared check.
    """


class MarketAssessment(StrictModel):
    kind: AssessmentKind = AssessmentKind.MARKET
    regime: MarketRegimeLabel
    score: int = Field(ge=0, le=100)
    risk_posture: str  # risk_on | risk_off | neutral
    reasons: list[str] = Field(default_factory=list)
    macro_notes: list[str] = Field(default_factory=list)


# ── Strategy → Risk ──────────────────────────────────────────────────────────


class TradeCandidate(StrictModel):
    """ONLY proposal shape Strategy Agent may emit. Not an order."""

    symbol: str = Field(min_length=1, max_length=16)
    action: TradeAction
    confidence: float = Field(ge=0.0, le=1.0)
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    target: Decimal = Field(gt=0)
    risk_reward: float = Field(gt=0)
    reasons: list[str] = Field(min_length=1)
    strategy_version: str
    technical_score: int | None = Field(default=None, ge=0, le=100)
    quant_score: int | None = Field(default=None, ge=0, le=100)
    news_label: str | None = None
    market_label: str | None = None
    pipeline_run_id: UUID | None = None
    exec_timeframe: Timeframe | None = None
    """The series the entry, stop and target were measured on.

    Geometry without its timeframe is not reproducible: an entry drawn on the
    hourly SMA20 and an exit judged against the daily SMA20 are two different
    numbers wearing one name, and the position agent proposed a sell eighteen
    seconds after a fill because it was reading the other one.
    """
    # F3 — thesis vs entry. Optional so legacy rows and tests stay valid.
    thesis: InstrumentThesis | None = None
    entry_decision: EntryDecision | None = None
    entry_quality: int | None = Field(default=None, ge=0, le=100)
    entry_quality_breakdown: dict[str, int] = Field(default_factory=dict)
    chase_reasons: list[str] = Field(default_factory=list)
    signal_price: Decimal | None = None
    """Last tradeable price known when the signal was formed (usually close)."""
    entry_zone_low: Decimal | None = None
    entry_zone_high: Decimal | None = None
    target_model: str | None = None
    target_reachability: TargetReachabilityClass | None = None
    session_cohort: SessionCohort | None = None

    @model_validator(mode="after")
    def validate_geometry(self) -> TradeCandidate:
        # V1: entry proposals are long-only BUY. Exits use ExitProposal, not this model.
        if self.action != TradeAction.BUY:
            raise ValueError(
                "V1 TradeCandidate allows BUY only (long-only); use ExitProposal for exits"
            )
        if not (self.stop < self.entry < self.target):
            raise ValueError("BUY requires stop < entry < target")
        return self


class PortfolioSnapshot(StrictModel):
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    open_exposure: Decimal
    open_positions: int
    day_pnl: Decimal
    week_pnl: Decimal
    drawdown_pct: float
    kill_switch: bool = False


class RiskLimits(StrictModel):
    max_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=5)
    max_position_pct: float = Field(default=5.0, gt=0, le=25)
    max_daily_loss_pct: float = Field(default=2.0, gt=0, le=20)
    max_weekly_loss_pct: float = Field(default=5.0, gt=0, le=40)
    max_portfolio_drawdown_pct: float = Field(default=10.0, gt=0, le=50)
    allow_leverage: bool = False
    allow_short: bool = False
    allow_options: bool = False

    # Concentration — several correlated names are one oversized position.
    max_open_positions: int = Field(default=8, ge=1, le=50)
    max_correlation: float = Field(default=0.80, gt=0, le=1.0)
    min_effective_positions: float = Field(default=2.0, ge=1.0, le=20.0)
    max_sector_pct: float = Field(default=25.0, gt=0, le=100)

    # Event risk — an earnings print is a coin flip, not a setup.
    block_days_before_earnings: int = Field(default=3, ge=0, le=30)
    block_days_after_earnings: int = Field(default=1, ge=0, le=30)

    require_earnings_check: bool = True
    require_news_check: bool = True
    """Refuse an entry whose headlines could not be read.

    On by default, because the strategy's negative-sentiment veto is only a
    veto while the feed behind it is readable. Turning it off is a deliberate
    decision to trade blind to news, and it is recorded on every decision taken
    under it via `limits_applied`.
    """
    """Refuse the entry when the earnings calendar could not be read.

    A stop does not protect a position through a print — the gap opens past it —
    so the windows above are the only defence, and they are worth nothing on a
    calendar nobody fetched. An unread calendar is therefore an unverified risk,
    not an absent one, and it is refused for the same reason an unverifiable
    spread is: see `LiquidityPolicy.require_live_spread`.

    Turning this off is a deliberate, recorded choice — it is stored in
    `RiskDecision.limits_applied`, so the audit trail shows every trade taken
    under it. Research and backtest callers do exactly that.
    """

    require_sector_check: bool = True
    """Refuse an entry whose sector could not be established.

    `max_sector_pct` above is only a cap on names we can classify. A name we
    cannot went into a shared `"unknown"` bucket, which let it bypass its own
    sector's limit entirely — the cap was written, tested, and not enforced for
    exactly the names a broad universe is made of.

    On by default. It costs nothing in the CORE tier, where every name is in
    `configs/universe.json` by construction, and it is what stops a broad
    universe from trading uncapped before a sector source exists. Turning it off
    is recorded in `limits_applied` like the other two.
    """


class RiskDecision(StrictModel):
    verdict: RiskVerdict
    reasons: list[str] = Field(default_factory=list)
    sized_qty: Decimal | None = None
    max_loss_usd: Decimal | None = None
    limits_applied: RiskLimits
    portfolio: PortfolioSnapshot
    candidate_id: UUID | None = None

    earnings_check: EarningsCheck = EarningsCheck.NOT_CHECKED
    """Whether the calendar was read for this decision, and if not, why not.

    A record rather than a mechanism: approval re-derives the calendar rather
    than trusting this. It is persisted so that afterwards it is answerable
    which decisions were made with event risk verified — a question the reasons
    list cannot answer, since a passing decision says only `RISK_OK`.
    """


class TradeOpportunity(StrictModel):
    id: UUID
    candidate: TradeCandidate
    risk: RiskDecision
    status: OpportunityStatus
    trading_mode: TradingMode
    created_at: datetime
    expires_at: datetime | None = None
    claimed_at: datetime | None = None
    # P1-10: scan size, approve size, and fill size are three facts. Collapsing
    # them into one `risk.sized_qty` let the operator approve one number while
    # another executed. All three are optional for legacy rows.
    proposed_qty: Decimal | None = None
    approved_qty: Decimal | None = None
    executed_qty: Decimal | None = None
    # F3 attribution crumbs persisted on the card as they become known.
    signal_detected_at: datetime | None = None
    signal_price: Decimal | None = None
    published_at: datetime | None = None
    published_price: Decimal | None = None
    approved_at: datetime | None = None
    approval_price: Decimal | None = None
    submitted_at: datetime | None = None
    submit_reference_price: Decimal | None = None
    filled_at: datetime | None = None
    fill_price: Decimal | None = None


class ConfirmationRequest(StrictModel):
    opportunity_id: UUID
    decision: UserDecision
    decided_at: datetime | None = None


class EntryTimingFacts(StrictModel):
    """Deterministic measurements. No LLM numbers."""

    current_price: float
    signal_price: float | None = None
    distance_from_vwap_pct: float | None = None
    distance_from_fast_ema_pct: float | None = None
    distance_from_slow_ema_pct: float | None = None
    atr: float | None = None
    atr_extension: float | None = None
    recent_impulse_return_pct: float | None = None
    recent_impulse_atr: float | None = None
    pullback_depth_pct: float | None = None
    nearest_support: float | None = None
    distance_to_support_pct: float | None = None
    nearest_resistance: float | None = None
    distance_to_resistance_pct: float | None = None
    relative_volume: float | None = None
    short_term_momentum_pct: float | None = None
    signal_to_current_drift_pct: float | None = None
    remaining_expected_reward_pct: float | None = None
    normal_expected_retrace_pct: float | None = None
    stop_distance_pct: float | None = None
    stop_distance_atr: float | None = None
    session_cohort: SessionCohort = SessionCohort.UNKNOWN


class EntryQualityBreakdown(StrictModel):
    price_location: int = Field(ge=0, le=100)
    vwap_location: int = Field(ge=0, le=100)
    atr_extension: int = Field(ge=0, le=100)
    pullback_quality: int = Field(ge=0, le=100)
    remaining_reward: int = Field(ge=0, le=100)
    support_structure: int = Field(ge=0, le=100)
    resistance_structure: int = Field(ge=0, le=100)
    short_term_momentum: int = Field(ge=0, le=100)
    volume_confirmation: int = Field(ge=0, le=100)
    market_alignment: int = Field(ge=0, le=100)
    liquidity_spread: int = Field(ge=0, le=100, default=70)
    signal_drift: int = Field(ge=0, le=100)

    def as_dict(self) -> dict[str, int]:
        return self.model_dump()

    @property
    def total(self) -> int:
        vals = list(self.as_dict().values())
        return int(round(sum(vals) / len(vals))) if vals else 0


class TargetPlan(StrictModel):
    price: Decimal
    model: str
    reachability: TargetReachabilityClass
    structure_target: Decimal | None = None
    atr_target: Decimal | None = None
    two_r_target: Decimal | None = None
    historical_mfe_target: Decimal | None = None
    historical_sample_size: int = 0
    resistance_before_target: bool = False
    reasons: list[str] = Field(default_factory=list)


class EntryDecisionBundle(StrictModel):
    """Structured thesis/entry/target/stop — prices are deterministic."""

    thesis: InstrumentThesis
    entry_decision: EntryDecision
    entry_quality: int = Field(ge=0, le=100)
    breakdown: EntryQualityBreakdown
    chase_reasons: list[str] = Field(default_factory=list)
    facts: EntryTimingFacts
    entry_zone_low: Decimal | None = None
    entry_zone_high: Decimal | None = None
    target: TargetPlan | None = None
    stop_price: Decimal | None = None
    normal_retrace_exceeds_stop: bool = False
    reasons: list[str] = Field(default_factory=list)


class EntryWatch(StrictModel):
    id: UUID
    symbol: str
    strategy_version: str
    created_at: datetime
    valid_until: datetime
    thesis: InstrumentThesis
    signal_price: Decimal
    current_price_at_creation: Decimal
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    planned_entry: Decimal
    planned_stop: Decimal
    planned_target: Decimal
    required_conditions: list[str] = Field(default_factory=list)
    invalidating_conditions: list[str] = Field(default_factory=list)
    max_spread_bps: float = 30.0
    minimum_reward_risk: float = 1.4
    entry_quality_at_creation: int = Field(ge=0, le=100)
    status: EntryWatchStatus = EntryWatchStatus.WAITING
    pipeline_run_id: UUID | None = None
    candidate: TradeCandidate | None = None
    reasons: list[str] = Field(default_factory=list)


class EntryAttribution(StrictModel):
    opportunity_id: UUID | None = None
    symbol: str
    signal_detected_at: datetime | None = None
    signal_price: Decimal | None = None
    candidate_created_at: datetime | None = None
    candidate_price: Decimal | None = None
    opportunity_published_at: datetime | None = None
    published_price: Decimal | None = None
    operator_approved_at: datetime | None = None
    approval_price: Decimal | None = None
    broker_submitted_at: datetime | None = None
    submit_reference_price: Decimal | None = None
    broker_filled_at: datetime | None = None
    fill_price: Decimal | None = None
    signal_to_publish_ms: float | None = None
    publish_to_approval_ms: float | None = None
    approval_to_submit_ms: float | None = None
    submit_to_fill_ms: float | None = None
    signal_to_fill_ms: float | None = None
    signal_to_fill_bps: float | None = None
    signal_to_fill_atr: float | None = None
    expected_60m_move_pct: float | None = None
    remaining_expected_move_at_fill_pct: float | None = None
    expected_move_consumed_fraction: float | None = None


class ShadowPolicyRecord(StrictModel):
    symbol: str
    recorded_at: datetime
    session_cohort: SessionCohort
    strategy_version: str
    pipeline_run_id: UUID | None = None
    old_policy: EntryDecision
    new_policy: EntryDecision
    thesis: InstrumentThesis
    signal_price: Decimal
    proposed_entry: Decimal
    proposed_stop: Decimal
    proposed_target: Decimal
    entry_quality: int | None = None
    chase_reasons: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class PipelineResult(StrictModel):
    """Supervisor + Stage 4 risk gate output — orders only after human approve."""

    pipeline_run_id: UUID
    symbol: str
    # completed | no_candidate | failed | risk_rejected | risk_passed |
    # awaiting_confirmation | position_open | wait_for_entry | no_trade
    # `risk_passed` means the candidate cleared risk but was not published — a
    # ranking pass evaluated it and has not yet decided whether to offer it.
    # `position_open` means the symbol was never analysed: the book already
    # holds it, so no entry proposal could have been acted on.
    # `wait_for_entry` means thesis valid but EntryTiming refused BUY_NOW.
    status: str
    technical: TechnicalAssessment | None = None
    news: NewsAssessment | None = None
    market: MarketAssessment | None = None
    candidate: TradeCandidate | None = None
    risk: RiskDecision | None = None
    opportunity: TradeOpportunity | None = None
    entry_decision: EntryDecisionBundle | None = None
    entry_watch: EntryWatch | None = None
    errors: list[str] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(default_factory=dict)


# ── Exits ────────────────────────────────────────────────────────────────────


class ExitProposal(StrictModel):
    position_id: UUID
    symbol: str
    action: TradeAction = TradeAction.SELL
    entry: Decimal
    current: Decimal
    pnl_pct: float
    reasons: list[str] = Field(min_length=1)
    recommendation: UserDecision  # SELL or HOLD
    confidence: float = Field(ge=0.0, le=1.0)


# ── Broker I/O (Execution Service only) ──────────────────────────────────────


class OrderRequest(StrictModel):
    client_order_id: str = Field(min_length=8, max_length=64)
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal = Field(gt=0)
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    opportunity_id: UUID | None = None
    position_id: UUID | None = None
    reason: str = Field(min_length=1)
    purpose: IntentPurpose = IntentPurpose.ENTRY
    """Whether this order takes on risk or sheds it.

    Carried down to the broker layer because that is where the kill switch is
    enforced, and a refusal there cannot otherwise tell an entry from the
    protective stop that guards a position already open. Defaults to `ENTRY`:
    an unlabelled order is treated as new exposure and refused when the desk is
    halted, so forgetting to set it fails in the safe direction.
    """

    @property
    def reduces_risk(self) -> bool:
        """Exits, emergency closes and protective stops. Never an entry.

        A halted desk still has to be able to defend and close what it already
        holds — the switch is pressed when something has gone wrong, which is
        exactly when open positions most need a stop and a way out.
        """
        return self.purpose is not IntentPurpose.ENTRY

    @field_validator("qty")
    @classmethod
    def qty_precision(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.00000001")):
            raise ValueError("qty precision too fine")
        return v


class OrderRecord(StrictModel):
    id: UUID
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    status: OrderStatus
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    filled_avg_price: Decimal | None = None
    filled_qty: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class FillRecord(StrictModel):
    order_id: UUID
    qty: Decimal
    price: Decimal
    filled_at: datetime
    fees: Decimal = Decimal(0)


class Position(StrictModel):
    id: UUID
    symbol: str
    qty: Decimal
    avg_entry: Decimal
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    status: PositionStatus
    opened_at: datetime
    closed_at: datetime | None = None
    realized_pnl: Decimal | None = None
    mark: Decimal | None = None
    """The venue's own valuation of this position, for display only.

    Carried because it arrives in the same response as the quantity and costs
    nothing extra, and because an operator looking at a list of open positions
    cannot otherwise tell which of them are working. `None` from any broker that
    does not report one — absent, never zero, and never a reason to skip a live
    quote.

    Not a market data read and not admissible as one. Every gate that reasons
    about price has an age limit and a source it will accept; this has neither,
    so it must not reach the risk engine, the liquidity gate or an exit rule. It
    is what the number on the screen is made of, and nothing else.
    """


class TradeJournalEntry(StrictModel):
    id: UUID
    position_id: UUID
    symbol: str
    entry: Decimal
    exit: Decimal
    qty: Decimal
    pnl: Decimal
    pnl_pct: float
    mfe: Decimal | None = None
    mae: Decimal | None = None
    max_drawdown_during: float | None = None
    entry_reasons: list[str]
    exit_reasons: list[str]
    strategy_version: str
    prompt_version: str | None = None
    trading_mode: TradingMode
    indicators_at_entry: dict[str, Any] = Field(default_factory=dict)
    assessments_at_entry: dict[str, Any] = Field(default_factory=dict)
    market_regime: str | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    risk_reward_planned: float | None = None
    bars_held: int | None = None


class BacktestTrade(StrictModel):
    """Closed trade produced by the backtest engine (pre-persistence)."""

    symbol: str
    entry: Decimal
    exit: Decimal
    stop: Decimal
    target: Decimal
    qty: Decimal
    pnl: Decimal
    pnl_pct: float
    mfe: Decimal
    mae: Decimal
    mfe_pct: float
    mae_pct: float
    entry_reasons: list[str]
    exit_reasons: list[str]
    strategy_version: str
    indicators_at_entry: dict[str, Any] = Field(default_factory=dict)
    opened_at: datetime
    closed_at: datetime
    bars_held: int
    risk_reward_planned: float
    costs: Decimal = Decimal(0)
    """Commission + regulatory fees + modelled spread/slippage for the round trip."""


class BacktestSummary(StrictModel):
    strategy_version: str
    symbol: str
    timeframe: Timeframe
    starting_equity: Decimal
    ending_equity: Decimal
    net_pnl: Decimal
    return_pct: float
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float
    avg_r: float | None
    avg_bars_held: float | None
    trades: list[BacktestTrade] = Field(default_factory=list)

    # Cost accounting — net_pnl above is always after costs.
    gross_pnl: Decimal = Decimal(0)
    total_costs: Decimal = Decimal(0)

    # Risk-adjusted performance
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    cagr_pct: float | None = None
    expectancy_usd: float | None = None
    expectancy_r: float | None = None
    avg_win_usd: float | None = None
    avg_loss_usd: float | None = None
    largest_loss_usd: float | None = None
    max_consecutive_losses: int = 0
    equity_curve: list[float] = Field(default_factory=list)

    # Benchmark (buy & hold over the same bars, same costs)
    benchmark_symbol: str | None = None
    benchmark_return_pct: float | None = None
    benchmark_max_drawdown_pct: float | None = None
    excess_return_pct: float | None = None


class AuditEvent(StrictModel):
    event_type: str
    actor: str
    pipeline_run_id: UUID | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
