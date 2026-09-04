"""ApprovalEvidence construction + sealed evaluate_final_approval.

Final Admission never trusts prior_admission, scanner, watch, UI, or LLM.
Only a fresh evaluation of FinalApprovalInput may mint BUY_ALLOWED.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Self
from uuid import UUID

from core.enums import AdmissionDecision, BrokerEnvironment, DataHealthStatus
from core.schemas import (
    AdmissionInput,
    ApprovalCommand,
    ApprovalEvidence,
    BrokerEvidence,
    EntryEvidence,
    EventRiskEvidence,
    FinalApprovalInput,
    GeometryEvidence,
    IdentityEvidence,
    LiquidityEvidence,
    MarketEvidence,
    PortfolioEvidence,
    RiskEvidence,
    SectorEvidence,
    TradeAdmissionResult,
)
from trading.approval_errors import DataBlockedError, NoTradeError
from trading.final_pretrade import require_final_admission
from trading.trade_admission import evaluate_from_admission_input
from trading.zone_arrival import ZoneArrivalFacts


@dataclass(frozen=True)
class FinalApprovalResult:
    admission: TradeAdmissionResult
    evidence: ApprovalEvidence
    fingerprint: str


def _require_block(name: str, present: bool, missing: list[str]) -> None:
    if not present:
        missing.append(name)


def _facts_from_mapping(raw: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    items: list[tuple[str, str]] = []
    for key in sorted(raw.keys(), key=str):
        val = raw[key]
        if isinstance(val, (dict, list)):
            items.append((str(key), json.dumps(val, sort_keys=True, default=str)))
        else:
            items.append((str(key), str(val)))
    return tuple(items)


def _status_name(obj: Any) -> str:
    if obj is None:
        return "missing"
    if hasattr(obj, "status"):
        st = obj.status
        return st.value if hasattr(st, "value") else str(st)
    if hasattr(obj, "value"):
        return str(obj.value)
    return str(obj)


def build_nested_evidence(
    inp: FinalApprovalInput,
    *,
    fingerprint: str,
) -> ApprovalEvidence:
    sealed = inp.admission_input
    market = sealed.market
    quote = sealed.quote
    stop_plan = sealed.stop_plan
    target_plan = sealed.target_plan

    identity = IdentityEvidence(
        request_id=inp.command.request_id,
        opportunity_id=inp.command.opportunity_id,
        expected_decision_version=inp.command.expected_decision_version,
        user_decision=inp.command.user_decision,
        requested_at=inp.command.requested_at,
        actor=inp.command.actor,
        decision_version=inp.command.expected_decision_version,
    )
    market_ev = MarketEvidence(
        regime=market.regime.value if market and market.regime else None,
        risk_posture=market.risk_posture if market else None,
        score=market.score if market else None,
        evaluated_at=market.evaluated_at if market else None,
        benchmark=market.benchmark if market else None,
        reason_codes=tuple(market.reasons) if market else (),
    )
    sector_ev = SectorEvidence(
        symbol=quote.symbol,
        sector=sealed.sector_label,
        industry=None,
        benchmark=sealed.sector_benchmark,
        tradable_long=sealed.sector_tradable,
        sector_regime=inp.sector_regime,
        data_status=inp.sector_data_status
        or ("healthy" if sealed.sector_tradable is not None else "unhealthy"),
        source_ts=sealed.sector_source_ts,
        bars_count=inp.sector_bars_count,
        reason_codes=inp.sector_reason_codes,
        classification_version=inp.sector_classification_version,
        assessment_version=inp.sector_assessment_version,
        provider=sealed.sector_provider,
    )
    entry_ev = EntryEvidence(
        symbol=quote.symbol,
        thesis=str(
            sealed.bundle.thesis.value
            if hasattr(sealed.bundle.thesis, "value")
            else sealed.bundle.thesis
        ),
        setup_type=str(
            sealed.setup_type.value if hasattr(sealed.setup_type, "value") else sealed.setup_type
        ),
        setup_quality=sealed.setup_quality,
        entry_quality=inp.entry_quality,
        quote_bid=quote.bid,
        quote_ask=quote.ask,
        quote_ts=quote.ts,
        bars_count=sealed.bars_count,
        bar_timeframe=sealed.bar_timeframe,
        last_bar_ts=sealed.last_bar_ts,
        entry_zone_low=sealed.entry_zone_low,
        entry_zone_high=sealed.entry_zone_high,
    )
    geometry_ev = GeometryEvidence(
        entry=inp.limit_price,
        stop=inp.stop_price,
        target=target_plan.price if target_plan else Decimal(0),
        sized_qty=inp.sized_qty,
        stop_provenance=inp.stop_provenance or (stop_plan.model if stop_plan else "structure"),
        target_provenance=inp.target_provenance,
        target_reachability=inp.target_reachability
        or (
            target_plan.reachability.value
            if target_plan and hasattr(target_plan.reachability, "value")
            else str(getattr(target_plan, "reachability", "unknown"))
        ),
        effective_rr=inp.effective_rr,
        spread=inp.spread,
        geometry_hash=inp.geometry_hash,
    )
    portfolio_ev = PortfolioEvidence(
        equity=_dec_or_none(sealed.portfolio_snapshot.get("equity")),
        cash=_dec_or_none(sealed.portfolio_snapshot.get("cash")),
        open_positions=int(sealed.portfolio_snapshot.get("open_position_count") or 0),
        verified=bool(sealed.portfolio_snapshot),
        kill_switch=bool(sealed.portfolio_snapshot.get("kill_switch")),
        facts=_facts_from_mapping(sealed.portfolio_snapshot),
    )
    risk_ev = RiskEvidence(
        verdict=inp.risk_verdict,
        sized_qty=inp.sized_qty,
        reasons=tuple(
            str(r)
            for r in (sealed.risk_snapshot.get("reasons") or ())
            if not isinstance(sealed.risk_snapshot.get("reasons"), dict)
        )
        if isinstance(sealed.risk_snapshot.get("reasons"), (list, tuple))
        else (),
        facts=_facts_from_mapping(sealed.risk_snapshot),
    )
    liquidity_ev = LiquidityEvidence(
        ok=inp.liquidity_ok,
        facts=_facts_from_mapping(sealed.liquidity_snapshot),
    )
    event_ev = EventRiskEvidence(
        news_status=_status_name(sealed.news_status),
        earnings_status=_status_name(sealed.earnings_status),
        news_blocked=False,
        earnings_blocked=False,
    )
    broker_ev = BrokerEvidence(
        broker=inp.broker,
        broker_account_id=inp.broker_account_id,
        broker_environment=inp.broker_environment,
        strategy_version=sealed.strategy_version,
        admission_version=sealed.admission_version,
        policy_version=sealed.policy_version,
        evaluated_at=sealed.evaluated_at,
    )
    return _SealedApprovalEvidence(
        identity=identity,
        market=market_ev,
        sector=sector_ev,
        entry=entry_ev,
        geometry=geometry_ev,
        portfolio=portfolio_ev,
        risk=risk_ev,
        liquidity=liquidity_ev,
        event_risk=event_ev,
        broker=broker_ev,
        request_fingerprint=fingerprint,
        command=inp.command,
        admission_input=sealed,
        geometry_hash=inp.geometry_hash,
        decision_version=inp.command.expected_decision_version,
        sized_qty=inp.sized_qty,
        limit_price=inp.limit_price,
        stop_price=inp.stop_price,
        risk_verdict=inp.risk_verdict,
        liquidity_ok=inp.liquidity_ok,
    )


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def canonical_evidence_payload(evidence: ApprovalEvidence) -> str:
    """Canonical JSON for fingerprint — excludes only wall-clock evaluated_at on broker."""
    raw = evidence.model_dump(mode="json")
    # Drop non-authority wall clock on broker block only; source timestamps stay.
    broker = raw.get("broker")
    if isinstance(broker, dict):
        broker.pop("evaluated_at", None)
    # admission_input.evaluated_at is wall-clock of the pass
    ai = raw.get("admission_input")
    if isinstance(ai, dict):
        ai.pop("evaluated_at", None)
    return json.dumps(raw, sort_keys=True, default=str)


def fingerprint_from_evidence(evidence: ApprovalEvidence) -> str:
    return hashlib.sha256(canonical_evidence_payload(evidence).encode()).hexdigest()[:32]


def evaluate_final_approval(
    *,
    command: ApprovalCommand,
    admission_input: AdmissionInput,
    geometry_hash: str,
    sized_qty: Decimal,
    limit_price: Decimal,
    stop_price: Decimal,
    risk_verdict: str,
    liquidity_ok: bool,
    broker: str,
    broker_account_id: str,
    broker_environment: str = BrokerEnvironment.PAPER.value,
    unresolved_broker_state: bool = False,
    sector_regime: str | None = None,
    sector_data_status: str | None = None,
    sector_bars_count: int = 0,
    sector_reason_codes: tuple[str, ...] = (),
    sector_assessment_version: str | None = None,
    sector_classification_version: str | None = None,
    effective_rr: float | None = None,
    spread: Decimal | None = None,
    stop_provenance: str = "structure",
    target_provenance: str = "plan",
    target_reachability: str | None = None,
    entry_quality: int = 0,
    zone_arrival: ZoneArrivalFacts | None = None,
    tape_last: float | None = None,
) -> FinalApprovalResult:
    """Sole capital-path gate that may mint BUY_ALLOWED ApprovalEvidence.

    Never accepts prior_admission. Always re-evaluates from sealed facts.
    """
    if command.user_decision.value != "approve":
        raise DataBlockedError("approval_command_not_approve")
    if (
        admission_input.opportunity_id is not None
        and command.opportunity_id != admission_input.opportunity_id
    ):
        raise DataBlockedError("opportunity_id_mismatch")
    if admission_input.request_id is not None and command.request_id != admission_input.request_id:
        raise DataBlockedError("request_id_mismatch")
    if command.expected_decision_version != admission_input.decision_version:
        from trading.approval_errors import StaleDecisionError

        raise StaleDecisionError(
            f"decision_version:{admission_input.decision_version}"
            f"!={command.expected_decision_version}"
        )

    if broker_environment != BrokerEnvironment.PAPER.value:
        from core.metrics import METRICS

        METRICS.counter(
            "broker_environment_blocked",
            help_text="Final Admission refused non-paper broker environment",
        )
        raise DataBlockedError("BROKER_ENVIRONMENT_BLOCKED")
    if not broker or not broker_account_id:
        raise DataBlockedError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
    if unresolved_broker_state:
        raise DataBlockedError("UNRESOLVED_BROKER_STATE")

    sealed = admission_input.model_copy(
        update={
            "request_id": command.request_id,
            "decision_version": command.expected_decision_version,
            "sized_qty": sized_qty,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "geometry_hash": geometry_hash,
            "portfolio_snapshot": dict(admission_input.portfolio_snapshot),
            "risk_snapshot": dict(admission_input.risk_snapshot),
            "liquidity_snapshot": dict(admission_input.liquidity_snapshot),
        }
    )

    missing: list[str] = []
    _require_block("risk_snapshot", bool(sealed.risk_snapshot), missing)
    _require_block("portfolio_snapshot", bool(sealed.portfolio_snapshot), missing)
    _require_block("liquidity_snapshot", bool(sealed.liquidity_snapshot), missing)
    _require_block("news_status", sealed.news_status is not None, missing)
    _require_block("earnings_status", sealed.earnings_status is not None, missing)
    _require_block(
        "sector",
        sealed.sector_tradable is not None and sealed.sector_source_ts is not None,
        missing,
    )
    _require_block("market", sealed.market is not None, missing)
    if missing:
        from core.metrics import METRICS

        METRICS.counter(
            "approval_evidence_rejected",
            help_text="Final Admission missing required evidence blocks",
        )
        raise DataBlockedError(",".join(missing))

    if sealed.sector_tradable is False:
        raise NoTradeError("SECTOR_BLOCKED")

    if not liquidity_ok:
        raise NoTradeError("liquidity_failed")
    if risk_verdict.lower() not in {"pass", "ok", "approved"}:
        raise NoTradeError(f"risk_{risk_verdict}")

    # Always re-evaluate — never trust a prior scanner/watch/pretrade admission.
    admission = require_final_admission(
        evaluate_from_admission_input(sealed, zone_arrival=zone_arrival, tape_last=tape_last)
    )
    if admission.decision is not AdmissionDecision.BUY_ALLOWED or not admission.admitted:
        raise NoTradeError(admission.decision.value)
    if admission.data_status is not DataHealthStatus.HEALTHY:
        raise DataBlockedError(f"data_status:{admission.data_status.value}")

    reach = target_reachability
    if reach is None and sealed.target_plan is not None:
        r = sealed.target_plan.reachability
        reach = r.value if hasattr(r, "value") else str(r)

    final_inp = FinalApprovalInput(
        command=command,
        admission_input=sealed,
        geometry_hash=geometry_hash,
        sized_qty=sized_qty,
        limit_price=limit_price,
        stop_price=stop_price,
        risk_verdict=risk_verdict,
        liquidity_ok=liquidity_ok,
        broker=broker,
        broker_account_id=broker_account_id,
        broker_environment=broker_environment,
        unresolved_broker_state=unresolved_broker_state,
        sector_regime=sector_regime,
        sector_data_status=sector_data_status,
        sector_bars_count=sector_bars_count,
        sector_reason_codes=sector_reason_codes,
        sector_assessment_version=sector_assessment_version,
        sector_classification_version=sector_classification_version,
        effective_rr=effective_rr,
        spread=spread,
        stop_provenance=stop_provenance,
        target_provenance=target_provenance,
        target_reachability=reach or "unknown",
        entry_quality=entry_quality or int(getattr(sealed.bundle, "entry_quality", 0) or 0),
    )

    # Provisional evidence for fingerprint (fingerprint field placeholder).
    provisional = build_nested_evidence(final_inp, fingerprint="0" * 32)
    # Rebuild fingerprint without the placeholder fingerprint field itself.
    raw = provisional.model_dump(mode="json")
    raw.pop("request_fingerprint", None)
    broker_block = raw.get("broker")
    if isinstance(broker_block, dict):
        broker_block.pop("evaluated_at", None)
    ai = raw.get("admission_input")
    if isinstance(ai, dict):
        ai.pop("evaluated_at", None)
    fp = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:32]
    evidence = build_nested_evidence(final_inp, fingerprint=fp)
    return FinalApprovalResult(admission=admission, evidence=evidence, fingerprint=fp)


class _SealedApprovalEvidence(ApprovalEvidence):
    """ApprovalEvidence that refuses authority-field model_copy updates."""

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        if update:
            raise TypeError("ApprovalEvidence is sealed; rebuild via evaluate_final_approval")
        return super().model_copy(update=update, deep=deep)


def evidence_evaluated_at(evidence: ApprovalEvidence) -> datetime:
    return evidence.broker.evaluated_at


def evidence_request_id(evidence: ApprovalEvidence) -> UUID:
    return evidence.identity.request_id
