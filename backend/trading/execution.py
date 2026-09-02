"""Execution service — ONLY code path that may call BrokerPort.place_order.

Lifecycle (paper desk):
  claim → entry order → wait fill → protective stop (hard fail → flatten)
  → ledger open at fill price → EXECUTED
Exit: claim → market sell → wait fill → journal at fill price

On recoverable entry/exit broker failures (reject, fill timeout), release the
claim back to awaiting_confirmation so the desk card stays actionable.
Post-fill stop failure still discards after flatten (no re-buy of naked long).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from broker.interface import BrokerRejection, broker_connection_state
from core.config import get_settings
from core.enums import (
    BrokerConnectionState,
    DataHealthStatus,
    IntentPurpose,
    IntentStatus,
    OpportunityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    Timeframe,
    TradeAction,
    UserDecision,
)
from core.ports import AuditPort, BrokerPort, MarketDataPort, QuotePort
from core.schemas import (
    Bar,
    ExitProposal,
    OrderRecord,
    OrderRequest,
    Quote,
    RiskDecision,
    TradeCandidate,
    TradeOpportunity,
)
from risk.context_builder import build_risk_context
from risk.kill_switch import is_kill_switch_on
from risk.risk_engine import RiskContext, RiskEngine
from trading.decision_pipeline import NEW_EXPOSURE_GATE_ORDER
from trading.exits import (
    EXIT_APPROVING,
    EXIT_AWAITING,
    EXIT_HELD,
    EXIT_SOLD,
    OPERATOR_CLOSE_REASON,
    ExitOpportunity,
)
from trading.fills import fill_price, wait_for_fill
from trading.gates import (
    SPREAD_UNAVAILABLE,
    GateResult,
    LiquidityPolicy,
    SpreadReading,
    check_bar_freshness,
    check_connectivity,
    check_instrument_eligibility,
    check_liquidity,
    check_reconciliation,
    check_rth,
    measure_spread,
)
from trading.intents import INTENTS, OrderIntentStorePort, apply_exit_to_ledger
from trading.ledger import ExitApplication
from trading.order_intent import (
    OrderIntent,
    can_transition,
    emergency_exit_idempotency_key,
    entry_idempotency_key,
    exit_idempotency_key,
    intent_status_for,
    locate_broker_order,
    protection_idempotency_key,
    reason_code,
    unresolved_exit_for,
)
from trading.pricing import ENTRY_BUFFER_BPS as _ENTRY_BUFFER_BPS
from trading.pricing import (
    round_equity_price,
    round_equity_qty,
    round_order_qty,
)
from trading.reconcile_supervisor import RECONCILE, max_reconciliation_age
from trading.session_hours import fill_wait_seconds
from trading.viability import LIVE as _VIABILITY_LIVE
from trading.viability import MAX_ENTRY_SLIPPAGE_R as _MAX_ENTRY_SLIPPAGE_R
from trading.viability import assess_buy_viability

logger = logging.getLogger(__name__)

_LIQUIDITY_BAR_LOOKBACK_DAYS = 90

ENTRY_BUFFER_BPS = _ENTRY_BUFFER_BPS
"""Re-exported from `trading.pricing`, where the exit rules read it too."""

MAX_ENTRY_SLIPPAGE_R = _MAX_ENTRY_SLIPPAGE_R
"""How much of the planned risk may be spent on getting in, as a fraction of it.

Approval moves the entry but not the stop or the target, so every cent paid
above the card lengthens the risk and shortens the reward at the same time. OXY
was drawn at 59.11 against a stop at 58.43 — 0.67 of risk — and filled at 59.97,
having paid 0.86 to enter, more than the whole distance it was risking. A 2:1
card arrived at the broker as 0.32:1: $128 of risk buying $40 of reward.
`PRICE_MOVED_PAST_SETUP` allowed it because the price had not passed the target,
only most of the way to it.

The bound is on the entry rather than on the resulting ratio because the
strategy builds its target at exactly two times risk. A card therefore reads 2.0
and never more, so a re-check demanding 2.0 after any upward repricing would
refuse every entry the desk ever takes — a bar that stops all trading is not a
stronger check, it is a broken one. Bounding the entry keeps the meaning the
doctrine asks for: past this line it is no longer the setup that was analysed,
because a pullback entry bought a quarter of its own risk above the pullback is
not one. A quarter is also what holds the trade recognisable — the arithmetic
puts the worst admissible trade at 1.4:1.
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


_LIVE_ORDER_STATES = frozenset({OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIAL})


def _is_live_protection(record: OrderRecord | None) -> bool:
    """Whether this order is still standing between the position and a gap down."""
    return record is not None and record.status in _LIVE_ORDER_STATES


def _failed_or_none(result: GateResult) -> GateResult | None:
    return None if result.passed else result


def _stop_price_of(ledger_row: Any) -> Decimal | None:
    if ledger_row is None or ledger_row.stop_price is None:
        return None
    return Decimal(str(ledger_row.stop_price))


def _perm_id(order: OrderRecord) -> str | None:
    """IB's permId, when the adapter surfaced one.

    Unlike orderId it survives reconnects and clientId changes, which makes it
    the only broker-side handle worth persisting for long-horizon recovery.
    """
    value = order.raw.get("permId") if order.raw else None
    return str(value) if value else None


def _as_quote_port(source: object) -> QuotePort | None:
    """Use the market-data adapter as a quote source only if it really is one.

    Duck-typing here keeps providers that serve bars only from being silently
    promoted into quote providers that return nothing.
    """
    return source if isinstance(source, QuotePort) else None


class OpportunityStorePort(Protocol):
    def get(self, opportunity_id: UUID) -> TradeOpportunity | None: ...

    def update(self, opp: TradeOpportunity) -> TradeOpportunity: ...

    def claim(
        self,
        opportunity_id: UUID,
        *,
        from_status: OpportunityStatus,
        to_status: OpportunityStatus,
    ) -> TradeOpportunity | None: ...


class ExitStorePort(Protocol):
    def get(self, exit_id: UUID) -> ExitOpportunity | None: ...

    def upsert(self, proposal: ExitProposal) -> ExitOpportunity: ...

    def update(self, item: ExitOpportunity) -> ExitOpportunity: ...

    def claim(
        self,
        exit_id: UUID,
        *,
        from_status: str,
        to_status: str,
    ) -> ExitOpportunity | None: ...


class ExecutionService:
    def __init__(
        self,
        broker: BrokerPort,
        audit: AuditPort,
        store: OpportunityStorePort,
        exit_store: ExitStorePort | None = None,
        risk_engine: RiskEngine | None = None,
        *,
        fill_timeout_sec: float | None = None,
        intents: OrderIntentStorePort | None = None,
        market_data: MarketDataPort | None = None,
        quotes: QuotePort | None = None,
        liquidity_policy: LiquidityPolicy | None = None,
        entry_buffer_bps: float = ENTRY_BUFFER_BPS,
        max_entry_slippage_r: float = MAX_ENTRY_SLIPPAGE_R,
        require_rth: bool = True,
        require_fresh_reconciliation: bool = True,
        max_reconciliation_age_sec: float | None = None,
        reconciliation_age: Callable[[], float | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.store = store
        self.exit_store = exit_store
        self.risk = risk_engine or RiskEngine()
        self.fill_timeout = (
            fill_timeout_sec if fill_timeout_sec is not None else fill_wait_seconds()
        )
        self.intents: OrderIntentStorePort = intents if intents is not None else INTENTS
        # Optional: without a market-data port the liquidity gate cannot measure
        # anything, so it reports itself as unrun rather than passing blind.
        self.market_data = market_data
        # A quote source is separate from the bar source because a spread can
        # only be measured from a live top of book, never inferred from a bar.
        self.quotes = quotes if quotes is not None else _as_quote_port(market_data)
        self.liquidity_policy = liquidity_policy or LiquidityPolicy()
        self.entry_buffer_bps = entry_buffer_bps
        self.max_entry_slippage_r = max_entry_slippage_r
        self.require_rth = require_rth
        # New exposure is refused against broker truth nobody has verified
        # lately. Injected rather than imported at the call site so that a unit
        # test which is not about reconciliation can turn it off explicitly,
        # instead of the gate being quietly unarmed for everyone by default —
        # which is how the liquidity gate came to be missing.
        self.require_fresh_reconciliation = require_fresh_reconciliation
        self.max_reconciliation_age_sec = (
            max_reconciliation_age_sec
            if max_reconciliation_age_sec is not None
            else max_reconciliation_age()
        )
        self.reconciliation_age = reconciliation_age or RECONCILE.age_seconds
        self._clock = clock or _utcnow

    @property
    def broker_name(self) -> str:
        return type(self.broker).__name__

    @property
    def connection_state(self) -> BrokerConnectionState:
        return broker_connection_state(self.broker)

    async def _risk_context(self, candidate: TradeCandidate) -> tuple[RiskContext, list[str]]:
        """Re-derive the facts the engine judges against, at the moment of the click.

        A card can sit for an hour. Regime, book, and calendar must be re-checked.
        Unknown or missing regime is fail-closed — never treated as permission.
        """
        from agents.market.agent import assess_market
        from trading.market_gate import evaluate_market_gate

        now = self._clock()
        market = await assess_market(get_settings().fred_api_key, now=now)
        gate = evaluate_market_gate(
            market,
            now=now,
            sector_label=market.sector_label,
            sector_tradable=market.sector_tradable,
            require_sector=True,
        )
        regime: bool | None
        if gate.status is DataHealthStatus.HEALTHY:
            regime = gate.tradable_long
        else:
            regime = None
        try:
            built = await build_risk_context(
                candidate.symbol.upper(),
                broker=self.broker,
                market_data=self.market_data,
                finnhub_api_key=get_settings().finnhub_api_key,
                regime_tradable=regime,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — refuse rather than guess
            return RiskContext(now=now), [f"risk context unavailable: {exc!r}"]
        notes = [*built.notes, *gate.reason_codes]
        return built.context, notes

    async def decide(
        self,
        opportunity_id: UUID,
        decision: UserDecision,
        *,
        qty: Decimal | None = None,
        request_id: UUID | None = None,
        expected_decision_version: int | None = None,
    ) -> TradeOpportunity:
        opp = self.store.get(opportunity_id)
        if opp is None:
            raise ValueError("opportunity_not_found")

        if decision == UserDecision.APPROVE and opp.status == OpportunityStatus.EXECUTED:
            return opp
        if decision == UserDecision.SKIP and opp.status == OpportunityStatus.SKIPPED:
            return opp

        if opp.status == OpportunityStatus.APPROVING:
            raise ValueError("invalid_status:approving")
        if opp.status != OpportunityStatus.AWAITING_CONFIRMATION:
            raise ValueError(f"invalid_status:{opp.status.value}")

        if decision == UserDecision.APPROVE:
            from uuid import uuid4

            from trading.approval_errors import StaleDecisionError

            # API refuses missing fields; direct service callers (tests / internal)
            # may omit them — synthesize a fresh request_id and bind the card version.
            if request_id is None:
                request_id = uuid4()
            if expected_decision_version is None:
                expected_decision_version = opp.decision_version
            if expected_decision_version != opp.decision_version:
                from core.metrics import METRICS

                METRICS.counter(
                    "stale_decision_rejected",
                    help_text="APPROVE rejected: card decision_version mismatch",
                )
                raise StaleDecisionError(
                    f"decision_version:{opp.decision_version}!={expected_decision_version}"
                )

        if opp.expires_at and datetime.now(UTC) > opp.expires_at:
            claimed = self.store.claim(
                opportunity_id,
                from_status=OpportunityStatus.AWAITING_CONFIRMATION,
                to_status=OpportunityStatus.EXPIRED,
            )
            if claimed is None:
                current = self.store.get(opportunity_id)
                if current and current.status == OpportunityStatus.EXECUTED:
                    return current
            raise ValueError("opportunity_expired")

        if decision == UserDecision.SKIP:
            claimed = self.store.claim(
                opportunity_id,
                from_status=OpportunityStatus.AWAITING_CONFIRMATION,
                to_status=OpportunityStatus.SKIPPED,
            )
            if claimed is None:
                current = self.store.get(opportunity_id)
                if current and current.status == OpportunityStatus.SKIPPED:
                    return current
                raise ValueError(f"invalid_status:{current.status.value if current else 'missing'}")
            await self.audit.append(
                "OpportunitySkipped",
                "user",
                {"opportunity_id": str(claimed.id)},
                pipeline_run_id=claimed.candidate.pipeline_run_id,
            )
            return claimed

        if decision != UserDecision.APPROVE:
            raise ValueError("unsupported_decision")

        if is_kill_switch_on():
            raise RuntimeError("KILL_SWITCH")

        # Declared gate order must stay loaded on the capital path. The list is
        # the DecisionPipeline contract; silently dropping an import would mean
        # gate order is again whatever decide() happens to call.
        assert NEW_EXPOSURE_GATE_ORDER, "decision pipeline gate order is empty"

        # Prechecks run while still AWAITING. The durable CAS into APPROVING
        # happens inside approval_commit together with Admission + Entry intent
        # so a crash cannot leave a linked admission without an intent (or the
        # reverse). Concurrent approvers both evaluate; exactly one wins the txn.
        portfolio = await self.broker.get_portfolio()
        portfolio = portfolio.model_copy(update={"kill_switch": is_kill_switch_on()})
        context, context_notes = await self._risk_context(opp.candidate)

        risk = self.risk.evaluate(
            opp.candidate,
            portfolio,
            candidate_id=opp.id,
            context=context,
        )
        if risk.verdict.value != "pass" or not risk.sized_qty:
            await self.audit.append(
                "RiskRejectOnApprove",
                "risk_engine",
                {**risk.model_dump(mode="json"), "context_notes": context_notes},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"RISK_REJECT:{','.join(risk.reasons)}")

        gate, bars = await self._pre_trade_gates(opp)
        if gate is not None:
            await self.audit.append(
                "RTHGateRejected" if gate.gate == "rth" else "LiquidityGateRejected",
                "execution",
                {"opportunity_id": str(opp.id), **gate.as_dict()},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"{gate.gate.upper()}_GATE_REJECTED:{','.join(gate.reasons)}")

        # Only now is there a price. The card's `entry` is the strategy's
        # pullback level and sits at or below the last close, so as a limit it
        # rests below the market and the fill window closes on it untouched.
        # Pricing the order against the live offer is what makes an approval an
        # entry rather than an eighteen-second wait.
        quote, spread = await self._top_of_book(opp.candidate.symbol)
        priced, pricing = self._priced_for_execution(opp.candidate, quote, spread)
        if priced is None:
            await self.audit.append(
                "LiquidityGateRejected",
                "execution",
                {"opportunity_id": str(opp.id), **pricing.as_dict()},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"LIQUIDITY_GATE_REJECTED:{','.join(pricing.reasons)}")

        from trading.final_admission import build_and_evaluate_final_admission
        from trading.final_pretrade import PretradeRejection

        if quote is None:
            await self.audit.append(
                "PretradeValidationRejected",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "code": "BUY_REJECTED_STALE_DATA",
                    "detail": "QUOTE_REQUIRED",
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError("BUY_REJECTED_STALE_DATA:QUOTE_REQUIRED")

        if self.market_data is None:
            raise RuntimeError("LIQUIDITY_GATE_REJECTED:MARKET_DATA_NOT_CONFIGURED")

        evaluated_at = self._clock()
        from agents.market.agent import assess_market
        from trading.sector_assessment import get_sector_assessment_port

        fresh_market = await assess_market(get_settings().fred_api_key, now=evaluated_at)
        sector = await get_sector_assessment_port().assess(priced.symbol, now=evaluated_at)
        if not sector.fresh or sector.tradable_long is None:
            from core.metrics import METRICS
            from trading.approval_errors import DataBlockedError

            METRICS.counter(
                "sector_data_blocked",
                help_text="APPROVE blocked: sector assessment missing or stale",
            )
            raise DataBlockedError(",".join(sector.reason_codes) or "SECTOR_ASSESSMENT_REQUIRED")
        try:
            final_eval = await build_and_evaluate_final_admission(
                priced,
                quote=quote,
                market_data=self.market_data,
                now=evaluated_at,
                market=fresh_market,
                sector_label=sector.sector_label,
                sector_tradable=sector.tradable_long,
                require_sector=True,
                opportunity_id=opp.id,
                decision_version=opp.decision_version,
            )
            approval_admission = final_eval.admission
            admission_input = final_eval.admission_input
        except PretradeRejection as exc:
            await self.audit.append(
                "PretradeValidationRejected",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "code": exc.code,
                    "detail": exc.detail,
                    "evaluated_at": evaluated_at.isoformat(),
                    "quote_ts": quote.ts.isoformat() if quote.ts else None,
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"{exc.code}:{exc.detail}") from exc

        if not final_eval.geometry_hash:
            raise RuntimeError("ADMISSION_REQUIRED:geometry_hash_required")

        # Sizing is re-derived at the price we will actually pay. Crossing the
        # spread shortens the distance to the stop, so the same dollar limit
        # buys fewer shares — and the engine, not this method, is what decides
        # how many. Skipping this would let an approval take more risk than the
        # scan approved, which is the one direction a re-check may never move.
        risk = self.risk.evaluate(priced, portfolio, candidate_id=opp.id, context=context)
        if risk.verdict.value != "pass" or not risk.sized_qty:
            await self.audit.append(
                "RiskRejectOnApprove",
                "risk_engine",
                {
                    **risk.model_dump(mode="json"),
                    "context_notes": context_notes,
                    "repriced_entry": str(priced.entry),
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"RISK_REJECT:{','.join(risk.reasons)}")

        max_qty = round_order_qty(risk.sized_qty)
        if qty is None:
            order_qty = max_qty
        else:
            order_qty = round_order_qty(qty)
            if order_qty < 1:
                await self.audit.append(
                    "OperatorQtyInvalid",
                    "execution",
                    {
                        "opportunity_id": str(opp.id),
                        "requested_qty": str(qty),
                        "max_qty": str(max_qty),
                    },
                    pipeline_run_id=opp.candidate.pipeline_run_id,
                )
                raise RuntimeError("OPERATOR_QTY_INVALID")
            if order_qty > max_qty:
                await self.audit.append(
                    "OperatorQtyAboveRisk",
                    "execution",
                    {
                        "opportunity_id": str(opp.id),
                        "requested_qty": str(order_qty),
                        "max_qty": str(max_qty),
                        "repriced_entry": str(priced.entry),
                    },
                    pipeline_run_id=opp.candidate.pipeline_run_id,
                )
                raise RuntimeError(f"OPERATOR_QTY_ABOVE_RISK:{order_qty}>{max_qty}")

        limit_px = round_equity_price(priced.entry)
        stop_px = round_equity_price(priced.stop)

        if order_qty <= 0:
            await self.audit.append(
                "EntrySizeBelowOneShare",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "symbol": opp.candidate.symbol,
                    "sized_qty": str(risk.sized_qty),
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError("SIZE_BELOW_ONE_SHARE")

        qty = order_qty
        liquidity = self._liquidity_gate(opp, bars, qty=qty, price=limit_px, spread=spread)
        if liquidity is not None:
            await self.audit.append(
                "LiquidityGateRejected",
                "execution",
                {"opportunity_id": str(opp.id), **liquidity.as_dict()},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"LIQUIDITY_GATE_REJECTED:{','.join(liquidity.reasons)}")

        blocker = self._unresolved_blocker(opp.candidate.symbol, opportunity_id=opp.id)
        if blocker is not None:
            await self.audit.append(
                "EntryBlockedByUnresolvedState",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "symbol": opp.candidate.symbol,
                    "blocking_intent_id": str(blocker.id),
                    "blocking_status": blocker.status.value,
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"UNRESOLVED_BROKER_STATE:{blocker.status.value}")

        from trading.external_positions import EXTERNAL_POSITIONS

        if opp.candidate.symbol.upper() in EXTERNAL_POSITIONS.blocking_symbols():
            await self.audit.append(
                "EntryBlockedByExternalPosition",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "symbol": opp.candidate.symbol,
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"EXTERNAL_POSITION_BLOCK:{opp.candidate.symbol.upper()}")

        held = self._open_position_for(opp.candidate.symbol)
        if held is not None:
            await self.audit.append(
                "EntryBlockedByOpenPosition",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "symbol": opp.candidate.symbol,
                    "open_position_id": str(held.id),
                    "open_qty": str(held.qty),
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"POSITION_ALREADY_OPEN:{opp.candidate.symbol.upper()}")

        from trading.admission_authority import AdmissionAuthorityError
        from trading.admission_records import StaleDecisionError
        from trading.approval_commit import commit_approval_bundle
        from trading.approval_errors import (
            DataBlockedError,
            EntryInFlightError,
            IdempotencyConflictError,
        )

        # Freeze full ApprovalEvidence facts into AdmissionInput before commit.
        portfolio_snap = portfolio.model_dump(mode="json")
        risk_snap = risk.model_dump(mode="json")
        liquidity_snap = {"ok": True, "qty": str(qty), "price": str(limit_px)}
        news_status = None
        earnings_status = None
        if context is not None:
            news_status = getattr(context, "news", None) or getattr(context, "news_status", None)
            earnings_status = getattr(context, "earnings", None) or getattr(
                context, "earnings_status", None
            )
        if news_status is None or earnings_status is None:
            raise DataBlockedError("news_or_earnings_missing_on_approval_evidence")

        from core.schemas import ApprovalCommand
        from trading.approval_errors import NoTradeError
        from trading.approval_evidence import evaluate_final_approval

        cmd = ApprovalCommand(
            request_id=request_id,
            opportunity_id=opp.id,
            expected_decision_version=opp.decision_version,
            requested_qty=qty,
            requested_at=evaluated_at,
            actor="user",
        )
        admission_input = admission_input.model_copy(
            update={
                "request_id": request_id,
                "opportunity_id": opp.id,
                "decision_version": opp.decision_version,
                "sector_label": sector.sector_label,
                "sector_tradable": sector.tradable_long,
                "sector_benchmark": sector.benchmark,
                "sector_provider": sector.provider,
                "sector_source_ts": sector.source_ts,
                "portfolio_snapshot": portfolio_snap,
                "risk_snapshot": risk_snap,
                "liquidity_snapshot": liquidity_snap,
                "news_status": news_status,
                "earnings_status": earnings_status,
                "sized_qty": qty,
                "limit_price": limit_px,
                "stop_price": stop_px,
                "geometry_hash": final_eval.geometry_hash or admission_input.geometry_hash,
            }
        )
        final_ok = evaluate_final_approval(
            command=cmd,
            admission_input=admission_input,
            geometry_hash=final_eval.geometry_hash or admission_input.geometry_hash,
            sized_qty=qty,
            limit_price=limit_px,
            stop_price=stop_px,
            risk_verdict=risk.verdict.value,
            liquidity_ok=True,
            prior_admission=approval_admission,
        )
        approval_admission = final_ok.admission
        admission_input = final_ok.evidence.admission_input
        fp = final_ok.fingerprint

        try:
            bundle = commit_approval_bundle(
                opportunity_id=opp.id,
                admission=approval_admission,
                admission_input=admission_input,
                geometry_hash=final_eval.geometry_hash,
                quote_ts=quote.ts,
                market_gate_ts=final_eval.market_gate.regime_ts,
                pipeline_run_id=opp.candidate.pipeline_run_id,
                broker_name=self.broker_name,
                qty=qty,
                limit_px=limit_px,
                stop_px=stop_px,
                risk_snapshot=risk_snap,
                strategy_version=opp.candidate.strategy_version,
                symbol=priced.symbol,
                opportunity_store=self.store,
                intent_store=self.intents,
                decision_version=opp.decision_version,
                request_id=request_id,
                request_fingerprint=fp,
            )
        except (
            StaleDecisionError,
            IdempotencyConflictError,
            EntryInFlightError,
            DataBlockedError,
            NoTradeError,
        ):
            raise
        except AdmissionAuthorityError:
            raise
        except ValueError:
            current = self.store.get(opportunity_id)
            if current and current.status == OpportunityStatus.EXECUTED:
                return current
            raise

        opp = bundle.opportunity
        intent = bundle.intent
        if bundle.created_intent:
            await self.audit.append(
                "OrderIntentCreated",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.symbol,
                    "requested_qty": str(intent.requested_qty),
                    "limit_price": str(limit_px),
                    "opportunity_id": str(opp.id),
                    "approval_admission_record_id": str(bundle.admission_record.id),
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order_intent",
                entity_id=str(intent.id),
            )

        try:
            entry_submitted, we_submitted = await self._place_entry(intent, opp)
            if not we_submitted:
                # Lost CREATED→SUBMITTING or resumed an intent another worker
                # already sent. Do not run fill → stop → ledger a second time —
                # that is how concurrent approvals left a BUY with no position
                # (or two stops) when both continued past recover.
                current = self.store.get(opportunity_id)
                if current and current.status == OpportunityStatus.EXECUTED:
                    return current
                raise ValueError("invalid_status:entry_in_flight")
            opp = opp.model_copy(
                update={
                    "submitted_at": datetime.now(UTC),
                    "submit_reference_price": limit_px,
                }
            )
            self.store.update(opp)
        except BrokerRejection as exc:
            self._release_opportunity(opp, risk)
            await self.audit.append(
                "EntryOrderRejected",
                "execution",
                {"error": str(exc), "opportunity_id": str(opp.id), "intent_id": str(intent.id)},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"ENTRY_ORDER_REJECTED:{exc}") from exc
        except ValueError:
            current = self.store.get(opportunity_id)
            if current and current.status == OpportunityStatus.EXECUTED:
                return current
            raise
        except Exception as exc:
            # Ambiguous failure: the order may be live at the broker. The
            # opportunity stays claimed so nothing re-enters this symbol until
            # reconciliation establishes what actually happened.
            await self.audit.append(
                "EntryStateUnknown",
                "execution",
                {"error": str(exc), "opportunity_id": str(opp.id), "intent_id": str(intent.id)},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            raise RuntimeError(f"ENTRY_STATE_UNKNOWN:{exc}") from exc

        try:
            entry_fill = await wait_for_fill(
                self.broker,
                entry_submitted.broker_order_id or "",
                timeout_sec=self.fill_timeout,
            )
        except RuntimeError as exc:
            settled, settled_status = await self._settle_stalled_entry(
                entry_submitted, intent, pipeline_run_id=opp.candidate.pipeline_run_id
            )
            if settled is None:
                if settled_status is IntentStatus.UNKNOWN:
                    # The card stays claimed: releasing it would invite a second
                    # entry while a possible fill is still unaccounted for.
                    raise RuntimeError(f"ENTRY_STATE_UNKNOWN:{exc}") from exc
                self._release_opportunity(opp, risk)
                await self.audit.append(
                    "EntryFillFailed",
                    "execution",
                    {"error": str(exc), "opportunity_id": str(opp.id)},
                    pipeline_run_id=opp.candidate.pipeline_run_id,
                )
                raise RuntimeError(f"ENTRY_FILL_FAILED:{exc}") from exc

            # Shares were bought before the order stalled. Carry on with the
            # smaller size so the position gets its protective stop.
            entry_fill = settled
            await self.audit.append(
                "EntryPartiallyFilled",
                "execution",
                {
                    "opportunity_id": str(opp.id),
                    "error": str(exc),
                    "ordered_qty": str(qty),
                    "filled_qty": str(entry_fill.filled_qty),
                    "order_id": entry_fill.broker_order_id,
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order",
                entity_id=entry_fill.broker_order_id,
            )

        await self.audit.append(
            "FillReceived",
            "execution",
            {
                "kind": "entry",
                "price": str(fill_price(entry_fill)),
                "qty": str(entry_fill.filled_qty or qty),
                "order_id": entry_fill.broker_order_id,
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
            entity_type="order",
            entity_id=entry_fill.broker_order_id,
        )

        filled_qty = round_equity_qty(entry_fill.filled_qty or qty)
        entry_px = round_equity_price(fill_price(entry_fill))

        # Record the fill, but leave the intent unresolved: it only becomes
        # FILLED once the resulting position is protected. A crash in between
        # therefore leaves a state that reconciliation is obliged to pick up.
        self.intents.update_fields(
            intent.id,
            filled_qty=filled_qty,
            average_fill_price=entry_px,
            last_broker_state=entry_fill.status.value,
        )

        # Through the same durable path reconciliation uses. Placing it inline
        # here with its own client id was how the entry-time stop and the
        # recovery-time stop came to be two different mechanisms with two
        # different failure behaviours for the same order.
        stop_order_id: str | None = None
        stop_error: str | None = None
        try:
            stop_order_id = await self._place_protective_stop(
                symbol=opp.candidate.symbol,
                qty=filled_qty,
                stop_price=stop_px,
                position_id=None,
                reason=f"protective stop for {opp.id}",
            )
        except Exception as exc:  # noqa: BLE001 — the venue could not be read at all
            stop_error = str(exc)

        if stop_order_id is not None:
            await self.audit.append(
                "OrderSubmitted",
                "execution",
                {
                    "kind": "protective_stop",
                    "broker_order_id": stop_order_id,
                    "symbol": opp.candidate.symbol,
                    "qty": str(filled_qty),
                    "stop_price": str(stop_px),
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order",
                entity_id=stop_order_id,
            )
        else:
            failure = stop_error or "protective stop not accepted"
            await self.audit.append(
                "StopOrderFailed",
                "execution",
                {"error": failure, "opportunity_id": str(opp.id)},
                pipeline_run_id=opp.candidate.pipeline_run_id,
            )
            # Hard fail: flatten immediately — never leave naked long
            flat = await self._emergency_flatten(
                symbol=opp.candidate.symbol,
                qty=filled_qty,
                pipeline_run_id=opp.candidate.pipeline_run_id,
                reason=f"stop_failed:{failure}",
            )
            if flat:
                self.intents.transition(
                    intent.id, IntentStatus.FILLED, last_error=f"stop_failed_flattened:{failure}"
                )
            else:
                # Filled, unprotected, and not provably flat. This is the worst
                # case, so it stays UNKNOWN and keeps the symbol blocked.
                self._mark_unknown(intent, f"stop_failed_flatten_unconfirmed:{failure}")
                await self.audit.append(
                    "EntryStateUnknown",
                    "execution",
                    {
                        "opportunity_id": str(opp.id),
                        "intent_id": str(intent.id),
                        "symbol": opp.candidate.symbol,
                        "note": "protective stop failed and flatten was not confirmed",
                    },
                    pipeline_run_id=opp.candidate.pipeline_run_id,
                )
            discarded = opp.model_copy(update={"status": OpportunityStatus.DISCARDED, "risk": risk})
            self.store.update(discarded)
            raise RuntimeError(f"STOP_FAILED_FLATTENED:{failure}")

        from trading.ledger import LEDGER

        ledger_row = LEDGER.open_from_opportunity(
            opp,
            qty=filled_qty,
            broker_entry_order_id=entry_fill.broker_order_id,
            fill_price=entry_px,
            stop_order_id=stop_order_id,
        )
        await self.audit.append(
            "PositionOpened",
            "execution",
            {
                "position_id": str(ledger_row.id),
                "symbol": ledger_row.symbol,
                "qty": str(filled_qty),
                "fill_price": str(entry_px),
                "planned_entry": str(opp.candidate.entry),
                "stop": str(opp.candidate.stop),
                "target": str(opp.candidate.target),
                "stop_order_id": stop_order_id,
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
            entity_type="position",
            entity_id=str(ledger_row.id),
        )

        # Protection is installed and the position is on the books: the intent
        # is now resolved and stops blocking this symbol. Recovery may already
        # have read FILLED off the broker, so this tolerates the no-op.
        self._safe_transition(intent, IntentStatus.FILLED)
        await self.audit.append(
            "OrderFilled",
            "execution",
            {
                "intent_id": str(intent.id),
                "symbol": opp.candidate.symbol,
                "filled_qty": str(filled_qty),
                "average_fill_price": str(entry_px),
                "protective_stop_order_id": stop_order_id,
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
            entity_type="order",
            entity_id=entry_fill.broker_order_id,
        )

        opp = opp.model_copy(
            update={
                "status": OpportunityStatus.EXECUTED,
                "risk": risk,
                "claimed_at": None,
                "approved_qty": qty,
                "executed_qty": filled_qty,
                "proposed_qty": opp.proposed_qty
                if opp.proposed_qty is not None
                else risk.sized_qty,
                "approved_at": opp.approved_at or opp.claimed_at,
                "approval_price": opp.approval_price or limit_px,
                "submitted_at": opp.submitted_at,
                "submit_reference_price": opp.submit_reference_price or limit_px,
                "filled_at": datetime.now(UTC),
                "fill_price": entry_px,
            }
        )
        self.store.update(opp)
        from trading.attribution import build_attribution

        attr = build_attribution(
            symbol=opp.candidate.symbol,
            opportunity_id=opp.id,
            signal_detected_at=opp.signal_detected_at or opp.created_at,
            signal_price=opp.signal_price or opp.candidate.signal_price or opp.candidate.entry,
            candidate_created_at=opp.created_at,
            candidate_price=opp.candidate.entry,
            opportunity_published_at=opp.published_at or opp.created_at,
            published_price=opp.published_price or opp.candidate.entry,
            operator_approved_at=opp.approved_at,
            approval_price=opp.approval_price,
            broker_submitted_at=opp.submitted_at or opp.approved_at,
            submit_reference_price=opp.submit_reference_price,
            broker_filled_at=opp.filled_at,
            fill_price=entry_px,
        )
        await self.audit.append(
            "OpportunityApproved",
            "user",
            {
                "opportunity_id": str(opp.id),
                "proposed_qty": str(opp.proposed_qty) if opp.proposed_qty is not None else None,
                "approved_qty": str(qty),
                "executed_qty": str(filled_qty),
                "qty": str(filled_qty),
                "entry_fill": str(entry_px),
                "entry_order_id": entry_fill.broker_order_id,
                "attribution": attr.model_dump(mode="json"),
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
        )
        return opp

    # ── Pre-execution gates ──────────────────────────────────────────────────

    @staticmethod
    def _open_position_for(symbol: str) -> Any:
        """The open ledger row for this symbol, if the book already holds one."""
        from trading.ledger import LEDGER

        return LEDGER.find_open_by_symbol(symbol)

    def _priced_for_execution(
        self,
        candidate: TradeCandidate,
        quote: Quote | None,
        spread: SpreadReading,
    ) -> tuple[TradeCandidate | None, GateResult]:
        """Re-price the card to a limit that executes against the live book.

        The strategy's `entry` is `min(SMA20, close)` — a level it wants to buy
        a dip at. Held for eighteen seconds it is not a dip order, it is a
        missed one, so the limit is moved to the offer plus a bounded buffer:
        marketable enough to cross, capped so a book that gaps away from us
        fills nothing rather than anything.

        Refusing without a live quote is the same rule the spread check already
        follows, restated here because it is now load-bearing twice: an order
        priced from a stale book is not marketable, it is arbitrary.

        Geometry is owned by `assess_buy_viability` so the desk preview and the
        click cannot disagree about whether the card still describes a trade.
        """
        viability = assess_buy_viability(
            candidate,
            quote,
            spread=spread,
            now=self._clock(),
            max_spread_bps=self.liquidity_policy.max_spread_bps,
            entry_buffer_bps=self.entry_buffer_bps,
            max_entry_slippage_r=self.max_entry_slippage_r,
            max_quote_age_sec=self.liquidity_policy.max_quote_age_sec,
        )
        measured = dict(viability.measured)

        if viability.state is not _VIABILITY_LIVE:
            thresholds: dict[str, object] = {}
            if "SPREAD_TOO_WIDE" in viability.reasons:
                thresholds["max_spread_bps"] = self.liquidity_policy.max_spread_bps
            if "ENTRY_TOO_FAR_ABOVE_CARD" in viability.reasons:
                thresholds["max_entry_slippage_r"] = self.max_entry_slippage_r
                thresholds["max_paid_above_card"] = measured.get("max_paid_above_card")
            return None, GateResult(
                gate="liquidity",
                passed=False,
                reasons=viability.reasons,
                measured=measured,
                thresholds=thresholds,
            )

        limit = Decimal(str(measured["limit_price"]))
        repriced_rr = float(measured["repriced_risk_reward"])
        repriced = candidate.model_copy(
            update={
                "entry": limit,
                "risk_reward": repriced_rr,
                "reasons": [
                    *candidate.reasons,
                    f"Entry repriced to the offer {limit} (card {candidate.entry})",
                ],
            }
        )
        return repriced, GateResult(gate="liquidity", passed=True, reasons=(), measured=measured)

    async def _top_of_book(self, symbol: str) -> tuple[Quote | None, SpreadReading]:
        """One read of the book, used to price the order and to judge the spread.

        Read twice, the order could be priced off one snapshot and cleared by a
        gate that measured another.
        """
        if self.quotes is None:
            return None, SPREAD_UNAVAILABLE
        try:
            quote = await self.quotes.get_quote(symbol)
        except Exception:
            logger.warning("execution: quote unavailable for %s", symbol, exc_info=True)
            return None, SPREAD_UNAVAILABLE
        return quote, measure_spread(
            quote,
            now=self._clock(),
            max_age_sec=self.liquidity_policy.max_quote_age_sec,
        )

    async def _pre_trade_gates(
        self,
        opp: TradeOpportunity,
    ) -> tuple[GateResult | None, list[Bar]]:
        """Everything refusable before the order has a price or a size.

        Split from the liquidity verdict because that one needs both, and both
        now come from the live book — but whether we may trade this symbol, at
        this hour, against a broker we have read recently does not. Asking those
        first keeps the reason a refused approval reports the most specific one:
        an entry attempted at midnight is refused for the hour, not for the
        empty book that the hour explains.

        The bars are returned rather than re-fetched, so the liquidity verdict
        and the freshness check that licenses it read the same series.
        """
        if self.require_rth:
            rth = check_rth(self._clock())
            if not rth.passed:
                return rth, []

        link = check_connectivity(self.connection_state)
        if not link.passed:
            return link, []

        eligible = check_instrument_eligibility(
            opp.candidate.symbol,
            allowed_symbols=self.liquidity_policy.allowed_symbols,
        )
        if not eligible.passed:
            return eligible, []

        if self.require_fresh_reconciliation:
            fresh = check_reconciliation(
                self.reconciliation_age(), max_age_sec=self.max_reconciliation_age_sec
            )
            if not fresh.passed:
                return fresh, []

        if self.market_data is None:
            # Not "nothing to measure, carry on". Spread, average dollar volume,
            # the price floor, participation and expected slippage are all
            # measured here and nowhere else in the system, so a service built
            # without a data port is a service with no liquidity gate at all.
            # Silence made that indistinguishable from a gate that ran and
            # passed, and the desk approved on it for as long as the wiring was
            # wrong. Naming it separately from an outage keeps the two apart:
            # this one is config, and it does not clear on its own.
            logger.error(
                "execution: no market-data port — the liquidity gate cannot run; "
                "build the service through api.deps.build_execution_service"
            )
            return GateResult(
                gate="liquidity",
                passed=False,
                reasons=("MARKET_DATA_NOT_CONFIGURED",),
                measured={"symbol": opp.candidate.symbol},
            ), []

        try:
            end = self._clock()
            bars = await self.market_data.get_bars(
                opp.candidate.symbol,
                Timeframe.D1,
                end - timedelta(days=_LIQUIDITY_BAR_LOOKBACK_DAYS),
                end,
            )
        except Exception:
            # Refusing to trade a symbol we cannot measure is the safe default.
            logger.warning(
                "execution: liquidity data unavailable for %s", opp.candidate.symbol, exc_info=True
            )
            return GateResult(
                gate="liquidity",
                passed=False,
                reasons=("MARKET_DATA_UNAVAILABLE",),
                measured={"symbol": opp.candidate.symbol},
            ), []

        # Before the liquidity verdict, not after: every number that verdict
        # rests on comes out of these bars, so a stale series does not produce a
        # cautious pass, it produces a confident one computed from last week.
        stale = check_bar_freshness(opp.candidate.symbol, bars, now=self._clock())
        if not stale.passed:
            return stale, []

        return None, bars

    def _liquidity_gate(
        self,
        opp: TradeOpportunity,
        bars: list[Bar],
        *,
        qty: Decimal,
        price: Decimal,
        spread: SpreadReading,
    ) -> GateResult | None:
        """The verdict that needs the finished order: its size and its price.

        Runs last, right before the broker call, so no caller can route around
        it — and on the price the order will actually carry, not the one the
        card was drawn with, because participation and slippage are properties
        of the order we are about to send.
        """
        return _failed_or_none(
            check_liquidity(
                opp.candidate.symbol,
                bars,
                qty=qty,
                price=price,
                spread=spread,
                policy=self.liquidity_policy,
            )
        )

    # ── Durable intent + idempotency ─────────────────────────────────────────

    def _unresolved_blocker(
        self,
        symbol: str,
        *,
        opportunity_id: UUID | None = None,
    ) -> OrderIntent | None:
        """An unresolved intent on this symbol forbids a conflicting new entry.

        Intents belonging to the same opportunity are excluded: those are the
        caller's own earlier attempt, which the idempotency path resumes rather
        than competes with. External/orphan incidents are checked separately.
        """
        ticker = symbol.upper()
        for candidate in self.intents.list_unresolved():
            if candidate.symbol.upper() != ticker:
                continue
            if opportunity_id is not None and candidate.opportunity_id == opportunity_id:
                continue
            return candidate
        return None

    async def _entry_intent(
        self,
        opp: TradeOpportunity,
        risk: RiskDecision,
        *,
        qty: Decimal,
        limit_px: Decimal,
        stop_px: Decimal,
    ) -> OrderIntent:
        """Find the live attempt for this opportunity, or durably record a new one."""
        existing = self.intents.list_by_key_prefix(f"entry:{opp.id}:")
        live = next((i for i in existing if i.is_unresolved), None)
        if live is not None:
            return live

        candidate = OrderIntent(
            idempotency_key=entry_idempotency_key(opp.id, len(existing)),
            broker=self.broker_name,
            broker_account_id=None,
            symbol=opp.candidate.symbol,
            side=OrderSide.BUY,
            requested_qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=limit_px,
            stop_price=stop_px,
            strategy_version=opp.candidate.strategy_version,
            opportunity_id=opp.id,
            risk_snapshot=risk.model_dump(mode="json"),
            approval_admission_record_id=opp.approval_admission_record_id,
            geometry_hash=opp.geometry_hash,
        )
        if candidate.approval_admission_record_id is None:
            from core.metrics import METRICS

            METRICS.counter(
                "entry_intent_without_admission",
                help_text="Refused entry intent: Opportunity has no ApprovalAdmission FK",
            )
            raise RuntimeError("ADMISSION_REQUIRED:opportunity_missing_approval_admission")
        intent, created = self.intents.create_or_get(candidate)
        if created:
            await self.audit.append(
                "OrderIntentCreated",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.symbol,
                    "requested_qty": str(intent.requested_qty),
                    "limit_price": str(limit_px),
                    "opportunity_id": str(opp.id),
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
        return intent

    async def _place_entry(
        self, intent: OrderIntent, opp: TradeOpportunity
    ) -> tuple[OrderRecord, bool]:
        """Submit the entry, or recover the one a previous attempt already sent.

        Returns `(record, submitted_by_caller)`. Only the caller that won
        CREATED→SUBMITTING and transmitted may drive fill settlement.
        """
        from trading.admission_authority import assert_entry_intent_has_admission

        # ApprovalAdmission is the sole authority — EntryDecision / Risk PASS /
        # TRIGGERED / Opportunity presence never authorize broker contact.
        assert_entry_intent_has_admission(intent, opp)

        if not intent.may_resubmit:
            # UNKNOWN after a lost reply: this process must adopt broker truth
            # and finish protection/ledger. SUBMITTING/SUBMITTED means another
            # worker still owns settle — refuse rather than double-open.
            if intent.status is IntentStatus.UNKNOWN:
                return await self._recover_entry(intent, opp), True
            raise ValueError("invalid_status:entry_in_flight")

        client_id = f"traido-e-{intent.id.hex[:16]}"
        request = OrderRequest(
            client_order_id=client_id,
            symbol=intent.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=intent.requested_qty,
            limit_price=intent.limit_price,
            opportunity_id=opp.id,
            reason=f"approved opportunity {opp.id}",
            purpose=intent.purpose,
        )
        # Persist the client id *before* transmitting: it is the handle that
        # lets recovery find the order if this process never sees the reply.
        # Compare-and-swap: only the worker that wins CREATED→SUBMITTING may
        # call place_order. The loser must not recover-and-settle in the same
        # request — the winner still owns fill → ledger.
        claimed = self.intents.transition_from(
            intent.id,
            from_status=IntentStatus.CREATED,
            to_status=IntentStatus.SUBMITTING,
            client_order_id=client_id,
        )
        if claimed is None:
            raise ValueError("invalid_status:entry_in_flight")
        intent = claimed
        await self.audit.append(
            "OrderSubmitStarted",
            "execution",
            {
                "intent_id": str(intent.id),
                "client_order_id": client_id,
                "symbol": intent.symbol,
                "qty": str(intent.requested_qty),
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
            entity_type="order_intent",
            entity_id=str(intent.id),
        )

        try:
            record = await self.broker.place_order(request)
        except BrokerRejection as exc:
            self.intents.transition(intent.id, IntentStatus.REJECTED, last_error=str(exc))
            await self.audit.append(
                "OrderRejected",
                "execution",
                {"intent_id": str(intent.id), "error": str(exc)},
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
            raise
        except Exception as exc:
            self._mark_unknown(intent, f"submit_failed:{exc}")
            raise

        self.intents.transition(
            intent.id,
            IntentStatus.SUBMITTED,
            broker_order_id=record.broker_order_id,
            last_broker_state=record.status.value,
        )
        await self.audit.append(
            "OrderSubmitted",
            "execution",
            {
                "kind": "entry",
                "intent_id": str(intent.id),
                **record.model_dump(mode="json"),
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
            entity_type="order",
            entity_id=record.broker_order_id,
        )

        acknowledged = intent_status_for(record.status, record.filled_qty)
        if acknowledged is IntentStatus.ACKNOWLEDGED:
            self.intents.transition(intent.id, IntentStatus.ACKNOWLEDGED)
            await self.audit.append(
                "OrderAcknowledged",
                "execution",
                {"intent_id": str(intent.id), "broker_order_id": record.broker_order_id},
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order",
                entity_id=record.broker_order_id,
            )
        return record, True

    async def _recover_entry(self, intent: OrderIntent, opp: TradeOpportunity) -> OrderRecord:
        """Adopt an order a previous attempt may already have placed.

        This is the branch that stops a retry, a restart, or a duplicated task
        from becoming a second position. It never submits.
        """
        await self.audit.append(
            "DuplicateOrderPrevented",
            "execution",
            {
                "intent_id": str(intent.id),
                "idempotency_key": intent.idempotency_key,
                "status": intent.status.value,
                "broker_order_id": intent.broker_order_id,
            },
            pipeline_run_id=opp.candidate.pipeline_run_id,
            entity_type="order_intent",
            entity_id=str(intent.id),
        )

        found = await self._locate_broker_order(intent)
        if found is None:
            # Absence from the open-order book does not prove the order never
            # existed — a filled order is not open either. Do not guess.
            self._mark_unknown(intent, "broker order not found while resuming intent")
            await self.audit.append(
                "EntryStateUnknown",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "symbol": intent.symbol,
                    "note": "resume found no broker order; reconciliation must resolve",
                },
                pipeline_run_id=opp.candidate.pipeline_run_id,
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
            raise RuntimeError("ENTRY_STATE_UNKNOWN:order_not_located")

        target = intent_status_for(found.status, found.filled_qty)
        current = self.intents.get(intent.id)
        if current is not None and current.status is not target:
            self.intents.transition(
                intent.id,
                target,
                broker_order_id=found.broker_order_id,
                last_broker_state=found.status.value,
            )
        else:
            self.intents.update_fields(
                intent.id,
                broker_order_id=found.broker_order_id,
                last_broker_state=found.status.value,
            )
        return found

    async def _locate_broker_order(self, intent: OrderIntent) -> OrderRecord | None:
        return await locate_broker_order(self.broker, intent)

    def _mark_unknown(self, intent: OrderIntent, reason: str) -> None:
        current = self.intents.get(intent.id)
        if current is None or current.status is IntentStatus.UNKNOWN:
            return
        if current.is_terminal:
            return
        self.intents.transition(intent.id, IntentStatus.UNKNOWN, last_error=reason)

    async def _settle_stalled_entry(
        self,
        submitted: OrderRecord,
        intent: OrderIntent,
        *,
        pipeline_run_id: UUID | None,
    ) -> tuple[OrderRecord | None, IntentStatus]:
        """Cancel a stalled entry and report whatever quantity actually filled.

        Cancelling a *partially* filled order only kills the remainder — the
        shares already bought are a live position. Reporting them lets the
        caller protect a smaller position instead of walking away from an
        unhedged long.

        Returns `(None, CANCELED)` when nothing filled, which is the ordinary
        timeout case, and `(None, UNKNOWN)` when the broker could not be read —
        the caller must not release the opportunity in that case.
        """
        oid = submitted.broker_order_id
        if not oid:
            self._mark_unknown(intent, "stalled entry has no broker order id")
            return None, IntentStatus.UNKNOWN

        self._safe_transition(intent, IntentStatus.CANCEL_PENDING)
        await self.audit.append(
            "OrderCancelRequested",
            "execution",
            {"intent_id": str(intent.id), "order_id": oid, "reason": "fill_timeout"},
            pipeline_run_id=pipeline_run_id,
            entity_type="order",
            entity_id=oid,
        )

        try:
            await self.broker.cancel_order(oid)
        except Exception:
            # Already terminal, or the broker is unreachable. Either way the
            # authoritative answer comes from re-reading the order below.
            logger.warning("execution: failed to cancel stalled entry %s", oid, exc_info=True)

        try:
            final = await self.broker.get_order(oid)
        except Exception:
            logger.exception(
                "execution: cannot read entry %s after cancel — possible unprotected fill", oid
            )
            self._mark_unknown(intent, "cancelled but unreadable")
            await self.audit.append(
                "EntryStateUnknown",
                "execution",
                {
                    "order_id": oid,
                    "intent_id": str(intent.id),
                    "note": "cancelled but unreadable; reconcile must verify",
                },
                pipeline_run_id=pipeline_run_id,
                entity_type="order",
                entity_id=oid,
            )
            return None, IntentStatus.UNKNOWN

        if (final.filled_qty or Decimal(0)) <= 0:
            self._safe_transition(
                intent, IntentStatus.CANCELED, last_broker_state=final.status.value
            )
            await self.audit.append(
                "OrderCancelled",
                "execution",
                {"intent_id": str(intent.id), "order_id": oid, "filled_qty": "0"},
                pipeline_run_id=pipeline_run_id,
                entity_type="order",
                entity_id=oid,
            )
            return None, IntentStatus.CANCELED

        # A missing average must not crash the path that protects a live
        # position; the limit we sent is the worst price we can have paid.
        if final.filled_avg_price is None or final.filled_avg_price <= 0:
            px = final.limit_price or submitted.limit_price
            if px and px > 0:
                final = final.model_copy(update={"filled_avg_price": px})

        self._safe_transition(
            intent,
            IntentStatus.PARTIALLY_FILLED,
            filled_qty=final.filled_qty or Decimal(0),
            last_broker_state=final.status.value,
        )
        await self.audit.append(
            "OrderPartiallyFilled",
            "execution",
            {
                "intent_id": str(intent.id),
                "order_id": oid,
                "filled_qty": str(final.filled_qty),
                "requested_qty": str(intent.requested_qty),
            },
            pipeline_run_id=pipeline_run_id,
            entity_type="order",
            entity_id=oid,
        )
        return final, IntentStatus.PARTIALLY_FILLED

    def _safe_transition(
        self,
        intent: OrderIntent,
        to_status: IntentStatus,
        **updates: object,
    ) -> None:
        """Move the intent along, tolerating a state the machine already left.

        Broker races mean the persisted state can be ahead of what this code
        path expects. That is not a reason to abort a capital-safety sequence.
        """
        current = self.intents.get(intent.id)
        if current is None or current.status is to_status:
            return
        if not can_transition(current.status, to_status):
            logger.warning(
                "execution: skipping transition %s -> %s for intent %s",
                current.status.value,
                to_status.value,
                intent.id,
            )
            return
        self.intents.transition(intent.id, to_status, **updates)

    def _release_opportunity(self, opp: TradeOpportunity, risk: RiskDecision) -> TradeOpportunity:
        """Return card to confirmation queue after a recoverable broker failure."""
        released = opp.model_copy(
            update={
                "status": OpportunityStatus.AWAITING_CONFIRMATION,
                "risk": risk,
                "claimed_at": None,
            }
        )
        self.store.update(released)
        return released

    async def _protection_intent(
        self,
        *,
        symbol: str,
        qty: Decimal,
        position_id: UUID | None,
        reason: str,
    ) -> tuple[OrderIntent, bool]:
        """The durable intent for one protective stop, or the unresolved one to resume.

        Mirrors `_emergency_intent`, and for the same reason. Phase 1 put the two
        side by side under an identical race: the emergency path, which writes an
        intent, produced one order; the protective path, which did not, produced
        two. The difference was never the code around them — it was the row.
        """
        live = unresolved_exit_for(
            self.intents.list_unresolved(),
            symbol=symbol,
            position_id=position_id,
            purposes=frozenset({IntentPurpose.PROTECTIVE_EXIT}),
        )
        if live is not None:
            return live, True

        position_key = str(position_id) if position_id else symbol.upper()
        prefix = f"protection:{position_key}:"
        generation = len(self.intents.list_by_key_prefix(prefix))
        intent, created = self.intents.create_or_get(
            OrderIntent(
                idempotency_key=protection_idempotency_key(position_key, generation),
                purpose=IntentPurpose.PROTECTIVE_EXIT,
                broker=self.broker_name,
                symbol=symbol.upper(),
                side=OrderSide.SELL,
                requested_qty=round_equity_qty(qty),
                order_type=OrderType.STOP,
                position_id=position_id,
                exit_reason=reason,
            )
        )
        if created:
            await self.audit.append(
                "ProtectiveIntentCreated",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.symbol,
                    "requested_qty": str(intent.requested_qty),
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
        return intent, not created

    async def _place_protective_stop(
        self,
        *,
        symbol: str,
        qty: Decimal,
        stop_price: Decimal,
        position_id: UUID | None,
        reason: str,
    ) -> str | None:
        """Send one protective stop, or adopt one a previous attempt already sent.

        Returns the stop's broker id, or None when the position is provably
        unprotected. `None` is a strong claim: it means the venue refused, not
        that we failed to hear back. Those were the same branch until Phase 1
        showed what it cost — a lost reply was read as a refusal, the caller
        flattened the position, and the stop the venue had in fact accepted was
        left resting above an account that no longer held the shares. One
        trigger away from a short, in a system that disables shorting.
        """
        intent, resumed = await self._protection_intent(
            symbol=symbol, qty=qty, position_id=position_id, reason=reason
        )
        if resumed or not intent.may_resubmit:
            adopted = await self._recover_protective_stop(intent.id, symbol=symbol)
            if adopted is not None:
                return adopted
            # The earlier attempt left nothing live at the venue — refused,
            # cancelled, or never sent. Retiring the intent is what lets the
            # next generation be created: a protective intent that outlives its
            # order would be resumed forever and the position would stay naked
            # while the book insisted it was covered.
            self.intents.transition(
                intent.id, IntentStatus.CANCELED, last_error="protection_not_live_at_broker"
            )
            intent, _ = await self._protection_intent(
                symbol=symbol, qty=qty, position_id=position_id, reason=reason
            )

        client_id = f"traido-s-{intent.id.hex[:16]}"
        request = OrderRequest(
            client_order_id=client_id,
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            qty=round_equity_qty(qty),
            stop_price=round_equity_price(stop_price),
            position_id=position_id,
            reason=reason,
            purpose=intent.purpose,
        )
        # Persisted before transmitting, exactly as on the entry path: this is
        # the handle recovery searches the venue with. Only the worker that
        # wins CREATED→SUBMITTING may place; the loser adopts.
        claimed = self.intents.transition_from(
            intent.id,
            from_status=IntentStatus.CREATED,
            to_status=IntentStatus.SUBMITTING,
            client_order_id=client_id,
        )
        if claimed is None:
            adopted = await self._recover_protective_stop(intent.id, symbol=symbol)
            if adopted is not None:
                return adopted
            raise RuntimeError("protective_stop_submit_race_unresolved")

        try:
            order = await self.broker.place_order(request)
        except BrokerRejection as exc:
            self.intents.transition(intent.id, IntentStatus.REJECTED, last_error=str(exc))
            await self.audit.append(
                "StopOrderFailed",
                "execution",
                {"symbol": symbol, "error": str(exc), "reason": reason, "qty": str(qty)},
                entity_type="position",
                entity_id=str(position_id) if position_id else None,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — ambiguous: the stop may be live
            self._mark_unknown(intent, f"protection_submit_failed:{exc}")
            await self.audit.append(
                "ProtectiveOrderUnknown",
                "execution",
                {
                    "symbol": symbol,
                    "intent_id": str(intent.id),
                    "client_order_id": client_id,
                    "error": str(exc),
                    "note": "stop may be resting at the broker; not treated as unprotected",
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
            return await self._recover_protective_stop(intent.id, symbol=symbol)

        self.intents.transition(
            intent.id,
            IntentStatus.SUBMITTED,
            broker_order_id=order.broker_order_id,
            broker_perm_id=_perm_id(order),
            last_broker_state=order.status.value,
        )
        return order.broker_order_id

    async def cancel_entry_order(
        self,
        *,
        broker_order_id: str,
        symbol: str,
        reason: str,
    ) -> bool:
        """Withdraw a resting entry order nobody is waiting on.

        Exposed for the orphan sweep, which used to call the broker directly.
        """
        if not broker_order_id:
            return False
        ok = await self._cancel_quietly(broker_order_id, note=reason)
        await self.audit.append(
            "OrphanEntryOrderCanceled" if ok else "OrphanEntryCancelFailed",
            "execution",
            {"symbol": symbol, "broker_order_id": broker_order_id, "reason": reason},
            entity_type="order",
            entity_id=broker_order_id,
        )
        return ok

    async def cancel_protection(
        self,
        *,
        broker_order_id: str,
        symbol: str,
        reason: str,
    ) -> bool:
        """Withdraw a protective order that should not be resting.

        Exposed for reconciliation, which finds excess protection but must not
        talk to the broker itself. Cancelling is a broker mutation like any
        other and belongs on the same side of the boundary as placing.
        """
        if not broker_order_id:
            return False
        await self.audit.append(
            "ExcessProtectionCancelling",
            "execution",
            {"symbol": symbol, "broker_order_id": broker_order_id, "reason": reason},
            entity_type="order",
            entity_id=broker_order_id,
        )
        ok = await self._cancel_quietly(broker_order_id, note=reason)
        self._retire_protective_intent(broker_order_id)
        return ok

    def _retire_protective_intent(self, broker_order_id: str) -> None:
        """Close the intent whose stop has just been cancelled.

        The intent and the order have to die together. Left open, the intent is
        the newest unresolved protective record for the position, so the next
        call to install protection resumes it, adopts a cancelled order and
        reports the position covered — the resize would silently become a
        removal.
        """
        for intent in self.intents.list_unresolved():
            if (
                intent.purpose is IntentPurpose.PROTECTIVE_EXIT
                and intent.broker_order_id == broker_order_id
            ):
                self.intents.transition(
                    intent.id, IntentStatus.CANCELED, last_error="superseded_by_resize"
                )
                return

    async def _recover_protective_stop(self, intent_id: UUID, *, symbol: str) -> str | None:
        """Find the stop this intent may already have placed, without sending another.

        Returns its broker id when the venue confirms one, `None` when the venue
        can be read and holds no such order. A venue that cannot be read at all
        raises, because "I could not look" must not reach a caller that reads
        `None` as "go ahead and flatten".

        The intent is re-read rather than taken from the caller's copy: the
        client id was written to the store moments ago by `transition`, and the
        in-memory object the caller is holding predates it. Recovering from the
        stale copy searches the venue for `None` and concludes, wrongly and
        expensively, that nothing was ever sent.
        """
        current = self.intents.get(intent_id)
        if current is None:
            return None
        if current.broker_order_id:
            live = await self.broker.get_order(current.broker_order_id)
            # Existence is not protection. A cancelled or filled stop is a
            # historical fact, and returning its id would report the position as
            # covered by an order the venue has already finished with.
            return current.broker_order_id if _is_live_protection(live) else None

        if not current.client_order_id:
            return None
        # Asked of the venue directly rather than by scanning the open-order
        # book: the adapter caches that book, and during a reconciliation pass
        # the cached copy predates the order we are looking for. Recovery would
        # then "confirm" that nothing was sent, moments after sending it.
        found = await self.broker.find_order_by_client_id(current.client_order_id)
        if found is None or not _is_live_protection(found):
            return None

        self.intents.transition(
            intent_id,
            IntentStatus.SUBMITTED,
            broker_order_id=found.broker_order_id,
            broker_perm_id=_perm_id(found),
            last_broker_state=found.status.value,
        )
        await self.audit.append(
            "ProtectiveOrderAdopted",
            "execution",
            {
                "symbol": symbol,
                "intent_id": str(intent_id),
                "broker_order_id": found.broker_order_id,
                "client_order_id": current.client_order_id,
            },
            entity_type="order",
            entity_id=found.broker_order_id,
        )
        return found.broker_order_id

    async def ensure_protection(
        self,
        *,
        symbol: str,
        qty: Decimal,
        stop_price: Decimal,
        position_id: UUID | None = None,
        reason: str,
    ) -> str | None:
        """Install a protective stop for an already-open position.

        Exposed for reconciliation, which finds unprotected positions but must
        not place orders itself — this service stays the only path to the
        broker. Returns the stop order id, or None if the position had to be
        emergency-closed instead.
        """
        oid = await self._place_protective_stop(
            symbol=symbol,
            qty=qty,
            stop_price=stop_price,
            position_id=position_id,
            reason=reason,
        )
        if oid is None:
            await self._emergency_flatten(
                symbol=symbol,
                qty=qty,
                pipeline_run_id=None,
                reason="protection_recovery_failed",
                position_id=position_id,
            )
            return None

        await self.audit.append(
            "ProtectiveOrderRecovered",
            "execution",
            {
                "symbol": symbol,
                "qty": str(qty),
                "stop_price": str(stop_price),
                "stop_order_id": oid,
                "reason": reason,
            },
            entity_type="position",
            entity_id=str(position_id) if position_id else None,
        )
        return oid

    async def resize_protection(
        self,
        *,
        symbol: str,
        position_id: UUID | None,
        remaining_qty: Decimal,
        stop_price: Decimal | None,
        reason: str,
        previous_stop_order_id: str | None = None,
    ) -> str | None:
        """Make the resting stop cover exactly the shares we still hold.

        Two failures are possible here and only one is acceptable. A stop that
        is *larger* than the position would sell shares we do not own, so the
        old one is always cancelled first. A stop that is missing leaves the
        remainder naked, so a failure to re-place it flattens the remainder
        rather than leaving it exposed.
        """
        await self.audit.append(
            "ProtectionResizeRequested",
            "execution",
            {
                "symbol": symbol,
                "position_id": str(position_id) if position_id else None,
                "remaining_qty": str(remaining_qty),
                "previous_stop_order_id": previous_stop_order_id,
                "reason": reason,
            },
            entity_type="position",
            entity_id=str(position_id) if position_id else None,
        )

        if previous_stop_order_id:
            await self._cancel_quietly(previous_stop_order_id, note="protection resize")
            self._retire_protective_intent(previous_stop_order_id)

        if remaining_qty <= 0:
            await self.audit.append(
                "ProtectionResized",
                "execution",
                {"symbol": symbol, "remaining_qty": "0", "stop_order_id": None},
                entity_type="position",
                entity_id=str(position_id) if position_id else None,
            )
            return None

        if stop_price is None:
            await self.audit.append(
                "ProtectionResizeFailed",
                "execution",
                {"symbol": symbol, "error": "no stop price on position", "reason": reason},
                entity_type="position",
                entity_id=str(position_id) if position_id else None,
            )
            await self._emergency_flatten(
                symbol=symbol,
                qty=remaining_qty,
                pipeline_run_id=None,
                reason="protection_resize_failed",
                position_id=position_id,
            )
            return None

        oid = await self._place_protective_stop(
            symbol=symbol,
            qty=remaining_qty,
            stop_price=stop_price,
            position_id=position_id,
            reason=f"resize after partial exit: {reason}",
        )
        if oid is None:
            await self.audit.append(
                "ProtectionResizeFailed",
                "execution",
                {"symbol": symbol, "remaining_qty": str(remaining_qty), "reason": reason},
                entity_type="position",
                entity_id=str(position_id) if position_id else None,
            )
            await self._emergency_flatten(
                symbol=symbol,
                qty=remaining_qty,
                pipeline_run_id=None,
                reason="protection_resize_failed",
                position_id=position_id,
            )
            return None

        if position_id is not None:
            from trading.ledger import LEDGER

            LEDGER.set_stop_order_id(position_id, oid)
        await self.audit.append(
            "ProtectionResized",
            "execution",
            {
                "symbol": symbol,
                "remaining_qty": str(remaining_qty),
                "stop_order_id": oid,
                "stop_price": str(stop_price),
            },
            entity_type="position",
            entity_id=str(position_id) if position_id else None,
        )
        return oid

    async def _cancel_quietly(self, broker_order_id: str, *, note: str) -> bool:
        """Best-effort cancel. An already-terminal order raising here is normal."""
        try:
            await self.broker.cancel_order(broker_order_id)
        except Exception:
            logger.warning(
                "execution: failed to cancel %s (%s)", broker_order_id, note, exc_info=True
            )
            return False
        return True

    async def _cancel_and_await_gone(
        self,
        broker_order_id: str,
        *,
        note: str,
        timeout_sec: float = 12.0,
    ) -> bool:
        """Cancel a resting order and wait until it no longer holds shares.

        Alpaca accepts the DELETE and still reports the stop as open for a beat
        (`held_for_orders`). A market exit sent in that window fails with
        insufficient qty even though we "cancelled".
        """
        await self._cancel_quietly(broker_order_id, note=note)
        terminal = {
            OrderStatus.CANCELED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                order = await self.broker.get_order(broker_order_id)
            except Exception:  # noqa: BLE001 — fall through to open-order sweep
                order = None
            if order is not None and order.status in terminal:
                return True
            try:
                open_ids = {
                    o.broker_order_id
                    for o in await self.broker.list_open_orders()
                    if o.broker_order_id
                }
            except Exception:  # noqa: BLE001
                open_ids = {broker_order_id}
            if broker_order_id not in open_ids:
                return True
            await asyncio.sleep(0.25)
        logger.warning("execution: order %s still open after cancel (%s)", broker_order_id, note)
        return False

    async def _free_shares_for_exit(self, *, symbol: str, stop_order_id: str | None) -> None:
        """Drop protective sells that pin the position before a market exit."""
        to_cancel: list[str] = []
        if stop_order_id:
            to_cancel.append(str(stop_order_id))
        try:
            for order in await self.broker.list_open_orders():
                if (
                    order.symbol.upper() == symbol.upper()
                    and order.side is OrderSide.SELL
                    and order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
                    and order.broker_order_id
                    and order.broker_order_id not in to_cancel
                ):
                    to_cancel.append(order.broker_order_id)
        except Exception:
            logger.warning(
                "execution: could not list open orders while freeing %s for exit",
                symbol,
                exc_info=True,
            )
        for oid in to_cancel:
            ok = await self._cancel_and_await_gone(oid, note="exit supersedes protective stop")
            if not ok:
                raise BrokerRejection(f"protective stop {oid} still holding {symbol} shares")

    async def _emergency_flatten(
        self,
        *,
        symbol: str,
        qty: Decimal,
        pipeline_run_id: UUID | None,
        reason: str,
        position_id: UUID | None = None,
    ) -> bool:
        """Sell out of a position we cannot protect.

        Emergency closes are the easiest duplicate to create: they fire from
        failure handlers, which are exactly the code paths that supervisors
        retry and concurrent workers re-enter. So this goes through the same
        durable intent as every other broker mutation, and an unresolved
        emergency exit for the position is *resumed*, never re-sent.

        Returns True only when the exit is *confirmed* filled. An unconfirmed
        flatten is not safety — the caller must keep the state unresolved.
        """
        await self.audit.append(
            "EmergencyCloseTriggered",
            "execution",
            {
                "symbol": symbol,
                "qty": str(qty),
                "reason": reason,
                "position_id": str(position_id) if position_id else None,
            },
            pipeline_run_id=pipeline_run_id,
        )

        intent, resumed = await self._emergency_intent(
            symbol=symbol, qty=qty, reason=reason, position_id=position_id
        )
        if resumed:
            return await self._resume_emergency(intent, pipeline_run_id=pipeline_run_id)

        client_id = f"traido-flat-{intent.id.hex[:16]}"
        claimed = self.intents.transition_from(
            intent.id,
            from_status=IntentStatus.CREATED,
            to_status=IntentStatus.SUBMITTING,
            client_order_id=client_id,
        )
        if claimed is None:
            refreshed = self.intents.get(intent.id)
            if refreshed is None:
                raise RuntimeError(f"order_intent_vanished:{intent.id}")
            return await self._resume_emergency(refreshed, pipeline_run_id=pipeline_run_id)
        try:
            order = await self.broker.place_order(
                OrderRequest(
                    client_order_id=client_id,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    qty=round_equity_qty(qty),
                    position_id=position_id,
                    reason=reason,
                    purpose=intent.purpose,
                )
            )
        except BrokerRejection as exc:
            self.intents.transition(intent.id, IntentStatus.REJECTED, last_error=str(exc))
            await self.audit.append(
                "EmergencyFlattenFailed",
                "execution",
                {"symbol": symbol, "error": str(exc), "reason": reason},
                pipeline_run_id=pipeline_run_id,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — ambiguous: the sell may be live
            self._mark_unknown(intent, f"emergency_submit_failed:{exc}")
            await self._audit_emergency_unknown(intent, symbol, str(exc), pipeline_run_id)
            return False

        self.intents.transition(
            intent.id,
            IntentStatus.SUBMITTED,
            broker_order_id=order.broker_order_id,
            broker_perm_id=_perm_id(order),
            last_broker_state=order.status.value,
        )
        await self.audit.append(
            "EmergencyExitSubmitted",
            "execution",
            {
                "symbol": symbol,
                "intent_id": str(intent.id),
                "broker_order_id": order.broker_order_id,
                "qty": str(qty),
                "reason": reason,
            },
            pipeline_run_id=pipeline_run_id,
            entity_type="order",
            entity_id=order.broker_order_id,
        )
        return await self._settle_emergency(intent, order, pipeline_run_id=pipeline_run_id)

    async def _emergency_intent(
        self,
        *,
        symbol: str,
        qty: Decimal,
        reason: str,
        position_id: UUID | None,
    ) -> tuple[OrderIntent, bool]:
        """Return the emergency intent to act on, and whether it is a resumption.

        Any unresolved emergency exit for this position counts, whatever failure
        first triggered it: two different reasons to flatten the same shares are
        still one flatten. A *completed* one counts too — those shares are
        already sold, and selling them again would open a short.
        """
        live = unresolved_exit_for(
            self.intents.list_unresolved(),
            symbol=symbol,
            position_id=position_id,
            purposes=frozenset({IntentPurpose.EMERGENCY_EXIT}),
        )
        if live is not None:
            return live, True

        done = self._completed_emergency_for(symbol, position_id)
        if done is not None:
            return done, True

        code = reason_code(reason)
        position_key = str(position_id) if position_id else symbol.upper()
        prefix = f"emergency_exit:{position_key}:{code}:"
        generation = len(self.intents.list_by_key_prefix(prefix))
        intent, created = self.intents.create_or_get(
            OrderIntent(
                idempotency_key=emergency_exit_idempotency_key(position_key, code, generation),
                purpose=IntentPurpose.EMERGENCY_EXIT,
                broker=self.broker_name,
                symbol=symbol.upper(),
                side=OrderSide.SELL,
                requested_qty=round_equity_qty(qty),
                order_type=OrderType.MARKET,
                position_id=position_id,
                exit_reason=reason,
            )
        )
        if created:
            await self.audit.append(
                "ExitIntentCreated",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "purpose": intent.purpose.value,
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.symbol,
                    "requested_qty": str(intent.requested_qty),
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
        return intent, not created

    def _completed_emergency_for(
        self,
        symbol: str,
        position_id: UUID | None,
    ) -> OrderIntent | None:
        """An emergency close that already flattened these shares."""
        ticker = symbol.upper()
        for intent in self.intents.list_by_key_prefix("emergency_exit:"):
            if intent.purpose is not IntentPurpose.EMERGENCY_EXIT:
                continue
            if intent.status is not IntentStatus.FILLED:
                continue
            if position_id is not None and intent.position_id != position_id:
                continue
            if position_id is None and intent.symbol.upper() != ticker:
                continue
            return intent
        return None

    async def _resume_emergency(
        self,
        intent: OrderIntent,
        *,
        pipeline_run_id: UUID | None,
    ) -> bool:
        """Adopt an emergency exit a previous attempt may already have sent."""
        await self.audit.append(
            "DuplicateOrderPrevented",
            "execution",
            {
                "intent_id": str(intent.id),
                "purpose": intent.purpose.value,
                "idempotency_key": intent.idempotency_key,
                "status": intent.status.value,
            },
            pipeline_run_id=pipeline_run_id,
            entity_type="order_intent",
            entity_id=str(intent.id),
        )
        found = await self._locate_broker_order(intent)
        if found is None:
            self._mark_unknown(intent, "emergency exit not locatable at broker")
            await self._audit_emergency_unknown(
                intent, intent.symbol, "no broker trace", pipeline_run_id
            )
            return False
        return await self._settle_emergency(intent, found, pipeline_run_id=pipeline_run_id)

    async def _settle_emergency(
        self,
        intent: OrderIntent,
        order: OrderRecord,
        *,
        pipeline_run_id: UUID | None,
    ) -> bool:
        """Confirm the flatten actually happened. Anything less is not safety."""
        if not order.broker_order_id:
            self._mark_unknown(intent, "emergency exit has no broker order id")
            await self._audit_emergency_unknown(
                intent, intent.symbol, "no broker order id", pipeline_run_id
            )
            return False

        try:
            filled = await wait_for_fill(
                self.broker, order.broker_order_id, timeout_sec=min(30.0, self.fill_timeout)
            )
        except Exception as fill_exc:  # noqa: BLE001
            await self.audit.append(
                "EmergencyFlattenUnconfirmed",
                "execution",
                {
                    "symbol": intent.symbol,
                    "intent_id": str(intent.id),
                    "error": str(fill_exc),
                },
                pipeline_run_id=pipeline_run_id,
            )
            # A flatten that stalled may still have sold part of the position.
            # Those shares are gone whether or not we noticed, so the book has
            # to absorb them before anyone reasons about what is left exposed.
            recovered = await self._read_after_cancel(
                intent, order.broker_order_id, reason=str(fill_exc)
            )
            if recovered is None or (recovered.filled_qty or Decimal(0)) <= 0:
                self._mark_unknown(intent, f"emergency_fill_unconfirmed:{fill_exc}")
                await self._audit_emergency_unknown(
                    intent, intent.symbol, str(fill_exc), pipeline_run_id
                )
                return False
            filled = recovered

        qty = filled.filled_qty or intent.requested_qty
        price = fill_price(filled)
        complete = qty >= intent.requested_qty
        self._safe_transition(
            intent,
            IntentStatus.FILLED if complete else IntentStatus.PARTIALLY_FILLED,
            filled_qty=qty,
            average_fill_price=price,
            last_broker_state=filled.status.value,
        )
        self._apply_exit_to_ledger(
            intent,
            filled_qty=qty,
            exit_price=price,
            reasons=["Emergency close", intent.exit_reason or "unspecified"],
        )
        await self.audit.append(
            "EmergencyFlattenFilled",
            "execution",
            {
                "symbol": intent.symbol,
                "intent_id": str(intent.id),
                "price": str(price),
                "filled_qty": str(qty),
                "requested_qty": str(intent.requested_qty),
            },
            pipeline_run_id=pipeline_run_id,
        )
        if not complete:
            # Part of the position we could not protect is still out there.
            await self._audit_emergency_unknown(
                intent, intent.symbol, "emergency close only partially filled", pipeline_run_id
            )
        return complete

    async def _audit_emergency_unknown(
        self,
        intent: OrderIntent,
        symbol: str,
        detail: str,
        pipeline_run_id: UUID | None,
    ) -> None:
        """Highest-severity signal: exposure we neither protected nor provably closed."""
        await self.audit.append(
            "EmergencyExitUnknown",
            "execution",
            {
                "symbol": symbol,
                "intent_id": str(intent.id),
                "severity": "critical",
                "note": detail,
            },
            pipeline_run_id=pipeline_run_id,
            entity_type="order_intent",
            entity_id=str(intent.id),
        )

    def _apply_exit_to_ledger(
        self,
        intent: OrderIntent,
        *,
        filled_qty: Decimal,
        exit_price: Decimal,
        reasons: list[str],
    ) -> ExitApplication:
        """Absorb only the part of this exit the book has not already seen."""
        return apply_exit_to_ledger(
            self.intents,
            intent,
            filled_qty=filled_qty,
            exit_price=exit_price,
            reasons=reasons,
        )

    async def decide_exit(
        self,
        exit_id: UUID,
        decision: UserDecision,
    ) -> ExitOpportunity:
        if self.exit_store is None:
            raise RuntimeError("exit_store_not_configured")

        item = self.exit_store.get(exit_id)
        if item is None:
            raise ValueError("exit_not_found")

        if decision == UserDecision.SELL and item.status == EXIT_SOLD:
            return item
        if decision == UserDecision.HOLD and item.status == EXIT_HELD:
            return item

        if item.status == EXIT_APPROVING:
            raise ValueError("invalid_status:approving")
        if item.status != EXIT_AWAITING:
            raise ValueError(f"invalid_status:{item.status}")

        if decision == UserDecision.HOLD:
            claimed = self.exit_store.claim(exit_id, from_status=EXIT_AWAITING, to_status=EXIT_HELD)
            if claimed is None:
                current = self.exit_store.get(exit_id)
                if current and current.status == EXIT_HELD:
                    return current
                raise ValueError(f"invalid_status:{current.status if current else 'missing'}")
            await self.audit.append(
                "ExitHeld",
                "user",
                {"exit_id": str(claimed.id), "symbol": claimed.proposal.symbol},
                entity_type="exit",
                entity_id=str(claimed.id),
            )
            return claimed

        if decision != UserDecision.SELL:
            raise ValueError("decision must be sell or hold")

        # Deliberately not gated on the kill switch. Selling out of a position
        # reduces risk, and the halt is normally pressed by the same operator
        # who is about to go and flatten the book; refusing here would leave
        # them the emergency path or the broker's terminal as the only routes
        # to flat. Entries stay refused, in `approve`.
        claimed = self.exit_store.claim(
            exit_id, from_status=EXIT_AWAITING, to_status=EXIT_APPROVING
        )
        if claimed is None:
            current = self.exit_store.get(exit_id)
            if current and current.status == EXIT_SOLD:
                return current
            raise ValueError(f"invalid_status:{current.status if current else 'missing'}")

        symbol = claimed.proposal.symbol
        position_id = claimed.proposal.position_id
        from trading.ledger import LEDGER

        gate = await self._exit_gate(claimed)
        if gate is not None:
            self._release_exit(claimed)
            raise RuntimeError(f"EXIT_BLOCKED:{','.join(gate.reasons)}")

        positions = await self.broker.list_positions()
        pos = next((p for p in positions if p.symbol.upper() == symbol.upper()), None)
        live = self._live_exit_intent(symbol, position_id)

        if pos is None and live is None:
            # Broker is flat and we have no exit in flight, so nothing we sent
            # is unaccounted for. Squaring the journal is safe here and only here.
            journal = LEDGER.close_and_journal(
                symbol=symbol,
                exit_price=claimed.proposal.current,
                exit_reasons=[*claimed.proposal.reasons, "Broker already flat"],
            )
            sold = claimed.model_copy(update={"status": EXIT_SOLD})
            self.exit_store.update(sold)
            await self.audit.append(
                "ExitSoldNoPosition",
                "execution",
                {
                    "exit_id": str(sold.id),
                    "symbol": symbol,
                    "journal_id": str(journal.id) if journal else None,
                },
                entity_type="exit",
                entity_id=str(sold.id),
            )
            if journal:
                await self._audit_journal(journal)
            return sold

        intent = live or await self._create_exit_intent(
            claimed,
            qty=round_equity_qty(pos.qty) if pos else round_equity_qty(claimed.proposal.entry * 0),
        )
        ledger_row = LEDGER.get(position_id) or LEDGER.find_open_by_symbol(symbol)
        stop_oid = (ledger_row.payload or {}).get("stop_order_id") if ledger_row else None

        try:
            submitted = await self._place_exit(intent, claimed, stop_order_id=stop_oid)
        except BrokerRejection as exc:
            self._release_exit(claimed)
            await self.audit.append(
                "ExitRejected",
                "execution",
                {"error": str(exc), "exit_id": str(exit_id), "intent_id": str(intent.id)},
                entity_type="exit",
                entity_id=str(exit_id),
            )
            raise RuntimeError(f"EXIT_ORDER_REJECTED:{exc}") from exc
        except Exception as exc:
            # Ambiguous. The sell may be live, so the card stays claimed rather
            # than inviting a human to press SELL a second time.
            await self._audit_exit_unknown(intent, exit_id, str(exc))
            raise RuntimeError(f"EXIT_STATE_UNKNOWN:{exc}") from exc

        return await self._settle_exit(intent, submitted, claimed, ledger_row=ledger_row)

    async def close_position(self, symbol: str) -> ExitOpportunity:
        """Sell a position because the operator said so, not because a rule did.

        Every other way out of a position needs an agent to have raised a card
        first, which made the desk's only sell button a side effect of a
        judgement it might never make: on 2026-08-31 the exit rule was corrected
        and the last card disappeared, leaving four open positions and no way to
        act on any of them. Protection still bounded the loss and the broker's
        own terminal still worked, but a desk that cannot close what it opened
        is not a desk.

        Deliberately not a second execution path. It synthesises the proposal
        that `decide_exit` already knows how to carry out, so the claim state
        machine, the durable intent, the sizing from broker truth and the ledger
        reduction are the ones that have been tested — a double click meets the
        same refusal it would meet on a card. A route that builds its own way to
        the broker is how the liquidity gate came to be unarmed.
        """
        if self.exit_store is None:
            raise RuntimeError("exit_store_not_configured")

        symbol = symbol.upper()
        positions = await self.broker.list_positions()
        pos = next((p for p in positions if p.symbol.upper() == symbol), None)
        if pos is None:
            raise ValueError(f"no_open_position:{symbol}")

        from trading.ledger import LEDGER

        ledger_row = LEDGER.find_open_by_symbol(symbol)
        current = await self._last_price(symbol, fallback=pos.avg_entry)
        pnl_pct = (
            float((current - pos.avg_entry) / pos.avg_entry * 100) if pos.avg_entry > 0 else 0.0
        )

        item = self.exit_store.upsert(
            ExitProposal(
                position_id=ledger_row.id if ledger_row else pos.id,
                symbol=symbol,
                action=TradeAction.SELL,
                entry=pos.avg_entry,
                current=current,
                pnl_pct=pnl_pct,
                reasons=[OPERATOR_CLOSE_REASON],
                recommendation=UserDecision.SELL,
                confidence=1.0,
            )
        )
        await self.audit.append(
            "PositionCloseRequested",
            "user",
            {"symbol": symbol, "exit_id": str(item.id), "qty": str(pos.qty)},
            entity_type="exit",
            entity_id=str(item.id),
        )
        return await self.decide_exit(item.id, UserDecision.SELL)

    async def _last_price(self, symbol: str, *, fallback: Decimal) -> Decimal:
        """A mark for the record. Never what the sell is priced from."""
        if self.quotes is not None:
            try:
                quote = await self.quotes.get_quote(symbol)
            except Exception:  # noqa: BLE001
                quote = None
            if quote is not None and quote.bid > 0:
                return quote.bid
        return fallback

    # ── Exit helpers ─────────────────────────────────────────────────────────

    async def _exit_gate(self, item: ExitOpportunity) -> GateResult | None:
        """Block a discretionary exit that could duplicate one already in flight.

        Two things disqualify it: a broker link we cannot trust, and an
        emergency flatten that already owns these shares. Neither blocks the
        emergency path itself — safety must always be able to act.
        """
        link = check_connectivity(self.connection_state)
        if not link.passed:
            await self.audit.append(
                "ExitBlockedByBrokerState",
                "execution",
                {"exit_id": str(item.id), "symbol": item.proposal.symbol, **link.as_dict()},
                entity_type="exit",
                entity_id=str(item.id),
            )
            return link

        blocker = unresolved_exit_for(
            self.intents.list_unresolved(),
            symbol=item.proposal.symbol,
            position_id=item.proposal.position_id,
            purposes=frozenset({IntentPurpose.EMERGENCY_EXIT}),
        )
        if blocker is not None:
            result = GateResult(
                gate="exit_conflict",
                passed=False,
                reasons=("EMERGENCY_EXIT_IN_FLIGHT",),
                measured={"blocking_intent_id": str(blocker.id), "status": blocker.status.value},
            )
            await self.audit.append(
                "ExitBlockedByUnresolvedState",
                "execution",
                {"exit_id": str(item.id), "symbol": item.proposal.symbol, **result.as_dict()},
                entity_type="exit",
                entity_id=str(item.id),
            )
            return result
        return None

    def _live_exit_intent(self, symbol: str, position_id: UUID) -> OrderIntent | None:
        return unresolved_exit_for(
            self.intents.list_by_key_prefix(f"exit:{position_id}:"),
            symbol=symbol,
            position_id=position_id,
            purposes=frozenset({IntentPurpose.EXIT}),
        )

    async def _create_exit_intent(self, item: ExitOpportunity, *, qty: Decimal) -> OrderIntent:
        position_id = item.proposal.position_id
        attempt = len(self.intents.list_by_key_prefix(f"exit:{position_id}:"))
        intent, created = self.intents.create_or_get(
            OrderIntent(
                idempotency_key=exit_idempotency_key(position_id, attempt),
                purpose=IntentPurpose.EXIT,
                broker=self.broker_name,
                symbol=item.proposal.symbol.upper(),
                side=OrderSide.SELL,
                requested_qty=qty,
                order_type=OrderType.MARKET,
                position_id=position_id,
                exit_reason="; ".join(item.proposal.reasons),
            )
        )
        if created:
            await self.audit.append(
                "ExitIntentCreated",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "purpose": intent.purpose.value,
                    "idempotency_key": intent.idempotency_key,
                    "exit_id": str(item.id),
                    "symbol": intent.symbol,
                    "requested_qty": str(qty),
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
        return intent

    async def _place_exit(
        self,
        intent: OrderIntent,
        item: ExitOpportunity,
        *,
        stop_order_id: str | None,
    ) -> OrderRecord:
        """Submit the exit, or adopt the one a previous attempt already sent."""
        if not intent.may_resubmit:
            await self.audit.append(
                "DuplicateOrderPrevented",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "purpose": intent.purpose.value,
                    "idempotency_key": intent.idempotency_key,
                    "status": intent.status.value,
                    "exit_id": str(item.id),
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
            found = await self._locate_broker_order(intent)
            if found is None:
                raise RuntimeError("exit order not locatable at broker")
            return found

        # The protective stop and the exit both want to sell the same shares.
        # Cancel must finish (not merely be requested) or Alpaca rejects the
        # market sell with insufficient qty / held_for_orders.
        await self._free_shares_for_exit(symbol=intent.symbol, stop_order_id=stop_order_id)

        client_id = f"traido-x-{intent.id.hex[:16]}"
        claimed = self.intents.transition_from(
            intent.id,
            from_status=IntentStatus.CREATED,
            to_status=IntentStatus.SUBMITTING,
            client_order_id=client_id,
        )
        if claimed is None:
            await self.audit.append(
                "DuplicateOrderPrevented",
                "execution",
                {
                    "intent_id": str(intent.id),
                    "purpose": intent.purpose.value,
                    "idempotency_key": intent.idempotency_key,
                    "exit_id": str(item.id),
                    "note": "lost_submit_cas",
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
            found = await self._locate_broker_order(intent)
            if found is None:
                refreshed = self.intents.get(intent.id)
                if refreshed is not None:
                    found = await self._locate_broker_order(refreshed)
            if found is None:
                raise RuntimeError("exit order not locatable at broker")
            return found
        await self.audit.append(
            "ExitSubmitStarted",
            "execution",
            {
                "intent_id": str(intent.id),
                "client_order_id": client_id,
                "symbol": intent.symbol,
                "qty": str(intent.requested_qty),
            },
            entity_type="order_intent",
            entity_id=str(intent.id),
        )

        try:
            record = await self.broker.place_order(
                OrderRequest(
                    client_order_id=client_id,
                    symbol=intent.symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    qty=intent.requested_qty,
                    position_id=intent.position_id,
                    reason=f"approved exit {item.id}",
                    purpose=intent.purpose,
                )
            )
        except BrokerRejection as exc:
            self.intents.transition(intent.id, IntentStatus.REJECTED, last_error=str(exc))
            raise
        except Exception as exc:
            self._mark_unknown(intent, f"exit_submit_failed:{exc}")
            raise

        self.intents.transition(
            intent.id,
            IntentStatus.SUBMITTED,
            broker_order_id=record.broker_order_id,
            broker_perm_id=_perm_id(record),
            last_broker_state=record.status.value,
        )
        await self.audit.append(
            "ExitSubmitted",
            "execution",
            {"intent_id": str(intent.id), **record.model_dump(mode="json")},
            entity_type="exit",
            entity_id=str(item.id),
        )
        if intent_status_for(record.status, record.filled_qty) is IntentStatus.ACKNOWLEDGED:
            self._safe_transition(intent, IntentStatus.ACKNOWLEDGED)
            await self.audit.append(
                "ExitAcknowledged",
                "execution",
                {"intent_id": str(intent.id), "broker_order_id": record.broker_order_id},
                entity_type="order_intent",
                entity_id=str(intent.id),
            )
        return record

    async def _settle_exit(
        self,
        intent: OrderIntent,
        submitted: OrderRecord,
        item: ExitOpportunity,
        *,
        ledger_row: Any,
    ) -> ExitOpportunity:
        """Turn whatever the broker did into local truth.

        Three outcomes, and the difference between them is the whole point of
        this method: the position closed, part of it closed and the remainder
        needs its protection resized, or we cannot tell — in which case nothing
        is assumed and the symbol stays blocked.
        """
        oid = submitted.broker_order_id or ""
        try:
            filled = await wait_for_fill(self.broker, oid, timeout_sec=self.fill_timeout)
        except RuntimeError as exc:
            final = await self._read_after_cancel(intent, oid, reason=str(exc))
            if final is None:
                await self._audit_exit_unknown(intent, item.id, str(exc))
                raise RuntimeError(f"EXIT_STATE_UNKNOWN:{exc}") from exc
            filled = final

        fill_qty = filled.filled_qty or Decimal(0)
        if fill_qty <= 0:
            self._safe_transition(
                intent, IntentStatus.CANCELED, last_broker_state=filled.status.value
            )
            await self.audit.append(
                "ExitCancelled",
                "execution",
                {"intent_id": str(intent.id), "exit_id": str(item.id), "filled_qty": "0"},
                entity_type="exit",
                entity_id=str(item.id),
            )
            # The stop was cancelled to make way for a sale that never happened.
            await self._restore_protection_after_failed_exit(item, ledger_row)
            self._release_exit(item)
            raise RuntimeError(f"EXIT_FILL_FAILED:{filled.status.value}")

        exit_px = fill_price(filled)
        applied = self._apply_exit_to_ledger(
            intent,
            filled_qty=fill_qty,
            exit_price=exit_px,
            reasons=list(item.proposal.reasons),
        )
        await self.audit.append(
            "FillReceived",
            "execution",
            {
                "kind": "exit",
                "price": str(exit_px),
                "qty": str(applied.filled_qty),
                "remaining_qty": str(applied.remaining_qty),
            },
            entity_type="exit",
            entity_id=str(item.id),
        )

        if applied.remaining_qty > 0:
            return await self._finish_partial_exit(intent, item, applied, filled, ledger_row)

        self._safe_transition(
            intent,
            IntentStatus.FILLED,
            filled_qty=applied.filled_qty,
            average_fill_price=exit_px,
            last_broker_state=filled.status.value,
        )
        await self.audit.append(
            "ExitFilled",
            "execution",
            {
                "intent_id": str(intent.id),
                "exit_id": str(item.id),
                "symbol": intent.symbol,
                "filled_qty": str(applied.filled_qty),
                "average_fill_price": str(exit_px),
            },
            entity_type="exit",
            entity_id=str(item.id),
        )
        sold = item.model_copy(update={"status": EXIT_SOLD})
        self.exit_store.update(sold) if self.exit_store else None
        await self.audit.append(
            "PositionClosed",
            "execution",
            {
                "symbol": intent.symbol,
                "exit_fill": str(exit_px),
                "journal_id": str(applied.journal.id) if applied.journal else None,
            },
            entity_type="position",
            entity_id=str(applied.position_id) if applied.position_id else None,
        )
        if applied.journal:
            await self._audit_journal(applied.journal)
        return sold

    async def _finish_partial_exit(
        self,
        intent: OrderIntent,
        item: ExitOpportunity,
        applied: ExitApplication,
        filled: OrderRecord,
        ledger_row: Any,
    ) -> ExitOpportunity:
        """A partial exit is not a failed exit — it is a smaller position."""
        await self.audit.append(
            "ExitPartiallyFilled",
            "execution",
            {
                "intent_id": str(intent.id),
                "exit_id": str(item.id),
                "symbol": intent.symbol,
                "filled_qty": str(applied.filled_qty),
                "requested_qty": str(intent.requested_qty),
                "remaining_qty": str(applied.remaining_qty),
            },
            entity_type="exit",
            entity_id=str(item.id),
        )
        self._safe_transition(
            intent,
            IntentStatus.PARTIALLY_FILLED,
            filled_qty=applied.filled_qty,
            average_fill_price=filled.filled_avg_price,
            last_broker_state=filled.status.value,
        )
        await self.resize_protection(
            symbol=intent.symbol,
            position_id=applied.position_id,
            remaining_qty=applied.remaining_qty,
            stop_price=_stop_price_of(ledger_row),
            reason=f"partial exit {item.id}",
            previous_stop_order_id=None,
        )
        # The broker order is dead; the fill it produced is now on the books and
        # protected, so the intent stops blocking. Its filled_qty keeps the fact.
        self._safe_transition(intent, IntentStatus.CANCELED)
        return self._release_exit(item)

    async def _restore_protection_after_failed_exit(
        self,
        item: ExitOpportunity,
        ledger_row: Any,
    ) -> None:
        if ledger_row is None:
            return
        await self.resize_protection(
            symbol=item.proposal.symbol,
            position_id=ledger_row.id,
            remaining_qty=Decimal(str(ledger_row.qty)),
            stop_price=_stop_price_of(ledger_row),
            reason=f"exit {item.id} did not fill",
            previous_stop_order_id=None,
        )

    async def _read_after_cancel(
        self,
        intent: OrderIntent,
        broker_order_id: str,
        *,
        reason: str,
    ) -> OrderRecord | None:
        """Cancel the remainder and re-read. None means broker truth is unreadable."""
        if not broker_order_id:
            self._mark_unknown(intent, "exit has no broker order id")
            return None

        self._safe_transition(intent, IntentStatus.CANCEL_PENDING)
        await self.audit.append(
            "ExitCancelRequested",
            "execution",
            {"intent_id": str(intent.id), "order_id": broker_order_id, "reason": reason},
            entity_type="order",
            entity_id=broker_order_id,
        )
        await self._cancel_quietly(broker_order_id, note="stalled exit")

        try:
            return await self.broker.get_order(broker_order_id)
        except Exception:
            logger.exception("execution: cannot read exit %s after cancel", broker_order_id)
            self._mark_unknown(intent, "exit cancelled but unreadable")
            return None

    async def _audit_exit_unknown(self, intent: OrderIntent, exit_id: UUID, detail: str) -> None:
        self._mark_unknown(intent, detail)
        await self.audit.append(
            "ExitStateUnknown",
            "execution",
            {
                "intent_id": str(intent.id),
                "exit_id": str(exit_id),
                "symbol": intent.symbol,
                "severity": "critical",
                "note": detail,
            },
            entity_type="order_intent",
            entity_id=str(intent.id),
        )

    def _release_exit(self, item: ExitOpportunity) -> ExitOpportunity:
        released = item.model_copy(update={"status": EXIT_AWAITING})
        if self.exit_store is not None:
            self.exit_store.update(released)
        return released

    async def _audit_journal(self, journal: Any) -> None:
        await self.audit.append(
            "TradeJournalFinalized",
            "execution",
            {
                "journal_id": str(journal.id),
                "symbol": journal.symbol,
                "pnl": str(journal.pnl),
                "pnl_pct": journal.pnl_pct,
                "entry": str(journal.entry),
                "exit": str(journal.exit),
            },
            entity_type="journal",
            entity_id=str(journal.id),
        )
