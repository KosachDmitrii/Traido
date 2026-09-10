"""Frozen session selection with an audited, versioned entry-policy migration."""

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.models.orb import OrbSessionRow
from database.session import session_factory


def read_session(day: str) -> dict[str, Any] | None:
    with session_factory()() as db:
        row = db.get(OrbSessionRow, day)
        return deepcopy(row.payload) if row else None


def create_session(day: str, payload: dict[str, Any], *, expand: bool = False) -> dict[str, Any]:
    with session_factory()() as db:
        row = db.scalar(select(OrbSessionRow).where(OrbSessionRow.session == day).with_for_update())
        if row:
            if expand and row.payload.get("selection_scope") != "all_qualified":
                # Preserve every existing plan and current state, including claims.
                previous = deepcopy(row.payload)
                merged = deepcopy(payload)
                merged["plans"] = {**merged.get("plans", {}), **previous.get("plans", {})}
                merged["states"] = {**merged.get("states", {}), **previous.get("states", {})}
                for key in ("entry_policy_rollout", "entry_policy_changes"):
                    if key in previous:
                        merged[key] = previous[key]
                merged["counts"]["selected"] = len(merged["plans"])
                merged["selection_expanded_from"] = len(previous.get("plans", {}))
                row.payload = merged
                db.commit()
            return deepcopy(row.payload)
        db.add(OrbSessionRow(session=day, payload=deepcopy(payload)))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.get(OrbSessionRow, day)
            if winner is None:
                raise
            return deepcopy(winner.payload)
        return deepcopy(payload)


def update_state(day: str, symbol: str, state: dict[str, Any]) -> None:
    with session_factory()() as db:
        row = db.scalar(select(OrbSessionRow).where(OrbSessionRow.session == day).with_for_update())
        if row is None or symbol not in row.payload.get("plans", {}):
            raise ValueError("ORB_PLAN_NOT_FOUND")
        payload = deepcopy(row.payload)
        current = payload.get("states", {}).get(symbol, {})
        payload.setdefault("states", {})[symbol] = {**current, **state}
        if current.get("opportunity_id"):
            payload["states"][symbol]["opportunity_id"] = current["opportunity_id"]
        row.payload = payload
        db.commit()


def upgrade_unpublished_entry_limits(day: str, *, now) -> dict[str, Any] | None:
    """Apply the user-authorized Paper rollout before approval is claimed.

    Publication takes the same row lock and compares the complete plan, so a
    candidate evaluated against an older limit cannot commit after this change.
    Unclaimed proposals are retired atomically and must be admitted again under
    a new ID. Approving, executed and unresolved claims are preserved. Plans
    are rebuilt from captured source bars.
    """
    from strategy.orb import PARAMETERS, VERSION, OrbPlan

    revision = PARAMETERS["entry_policy_revision"]
    with session_factory()() as db:
        row = db.scalar(select(OrbSessionRow).where(OrbSessionRow.session == day).with_for_update())
        if row is None:
            return None
        payload = deepcopy(row.payload)
        if payload.get("entry_policy_rollout") == revision:
            return payload
        revisions = []
        for symbol, raw in payload.get("plans", {}).items():
            state = payload.get("states", {}).get(symbol, {})
            plan = OrbPlan.model_validate(raw)
            pending = None
            if state.get("opportunity_id"):
                from uuid import UUID

                from core.enums import OpportunityStatus
                from database.models.desk import OpportunityRow

                try:
                    oid = UUID(state["opportunity_id"])
                except ValueError:
                    continue
                pending = db.scalar(
                    select(OpportunityRow).where(OpportunityRow.id == oid).with_for_update()
                )
                if (
                    pending is None
                    or pending.status != OpportunityStatus.AWAITING_CONFIRMATION.value
                ):
                    continue
            if pending is not None:
                from trading.opportunities import _from_row

                pending_opp = _from_row(pending)
                if (
                    pending_opp.submitted_at is not None
                    or pending_opp.auto_trigger_last_outcome == "UNKNOWN"
                ):
                    continue
            if now >= plan.entry_deadline:
                continue
            previous_parameters = plan.evidence.get("parameters", {})
            if previous_parameters.get("entry_policy_revision") == revision:
                continue
            from core.schemas import Bar
            from strategy.orb import form_plan

            rebuilt = form_plan(
                symbol,
                [Bar.model_validate(b) for b in plan.evidence.get("daily", [])],
                [Bar.model_validate(b) for b in plan.evidence.get("opening", [])],
                now=now,
                feed=plan.source.removeprefix("alpaca:"),
                version=VERSION,
            )
            if rebuilt.plan is None:
                continue
            updated = rebuilt.plan.model_dump(mode="json")
            change = {
                "symbol": symbol,
                "revision": revision,
                "at": now.isoformat(),
                "old_trigger": str(plan.trigger),
                "new_trigger": str(rebuilt.plan.trigger),
                "old_max_entry": str(plan.max_entry),
                "new_max_entry": str(rebuilt.plan.max_entry),
                "previous_parameters": previous_parameters,
                "old_version": plan.version,
                "new_version": VERSION,
                "reason": "USER_REQUESTED_PAPER_ENTRY_SIMPLIFICATION",
            }
            updated["evidence"]["entry_policy_change"] = change
            if pending is not None:
                from trading.opportunities import _from_row, _write_payload

                retired = _from_row(pending).model_copy(
                    update={
                        "status": OpportunityStatus.DISCARDED,
                        "auto_trigger_retry_at": None,
                        "auto_trigger_last_error": "ORB_ENTRY_POLICY_REPLACED",
                    }
                )
                _write_payload(db, retired)
                state = deepcopy(state)
                state.setdefault("replaced_opportunity_ids", []).append(state.pop("opportunity_id"))
                state.update(
                    state="WAIT", reasons=["ORB_WAITING_PULLBACK"], rearmed_at=now.isoformat()
                )
                payload["states"][symbol] = state
                change["replaced_opportunity_id"] = str(pending.id)
            payload["plans"][symbol] = updated
            revisions.append(change)
        payload["version"] = VERSION
        payload["entry_policy_rollout"] = revision
        payload["entry_policy_changes"] = [*payload.get("entry_policy_changes", []), *revisions]
        payload["parameters"] = deepcopy(PARAMETERS)
        row.payload = payload
        db.commit()
        return payload


def rearm_skipped_plan(day: str, symbol: str, opportunity_id: str, quote, *, now) -> bool:
    """Release only a durable SKIPPED claim after cooldown and a fresh price reset.

    A new proposal must pass the entire publication/admission path with a new ID.
    Never release an executed, approving or unresolved claim.
    """
    from datetime import datetime, timedelta
    from uuid import UUID

    from core.enums import OpportunityStatus
    from database.models.desk import OpportunityRow
    from strategy.orb import OrbPlan, evaluate_trigger

    with session_factory()() as db:
        row = db.scalar(select(OrbSessionRow).where(OrbSessionRow.session == day).with_for_update())
        if row is None:
            return False
        payload = deepcopy(row.payload)
        state = payload.get("states", {}).get(symbol, {})
        if state.get("opportunity_id") != opportunity_id:
            return False
        opp = db.get(OpportunityRow, UUID(opportunity_id))
        if opp is None or opp.status != OpportunityStatus.SKIPPED.value:
            return False
        plan = OrbPlan.model_validate(payload["plans"][symbol])
        if now >= plan.entry_deadline:
            return False
        if not state.get("skip_rearm_after"):
            state.update(
                state="WAIT",
                reasons=["ORB_SKIPPED_WAITING_RESET"],
                skip_rearm_after=(now + timedelta(seconds=60)).isoformat(),
            )
            payload["states"][symbol] = state
            row.payload = payload
            db.commit()
            return False
        # A skipped plan must reset outside the new buy band before a fresh entry.
        from core.config import get_settings
        from core.enums import BrokerEnvironment
        from core.schemas import Bar
        from strategy.orb import VERSION, form_plan

        reset_plan = plan
        if get_settings().broker_env is BrokerEnvironment.PAPER and plan.version != VERSION:
            rebuilt = form_plan(
                symbol,
                [Bar.model_validate(b) for b in plan.evidence.get("daily", [])],
                [Bar.model_validate(b) for b in plan.evidence.get("opening", [])],
                now=now,
                feed=plan.source.removeprefix("alpaca:"),
            )
            if rebuilt.plan is None:
                return False
            reset_plan = rebuilt.plan
        decision = evaluate_trigger(reset_plan, quote, now=now)
        reset_reason = (
            "ORB_WAITING_PULLBACK" if reset_plan.version == VERSION else "ORB_WAITING_BREAKOUT"
        )
        if now < datetime.fromisoformat(state["skip_rearm_after"]) or decision.reasons != [
            reset_reason
        ]:
            return False
        # A skipped proposal cannot have reached the broker: SKIP and APPROVE share CAS.
        state.setdefault("skipped_opportunity_ids", []).append(opportunity_id)
        state.pop("opportunity_id")
        state.pop("skip_rearm_after", None)
        state.update(state="WAIT", reasons=[reset_reason], rearmed_at=now.isoformat())
        payload["states"][symbol] = state
        # Permit a new, versioned geometry rollout only after the skipped claim is released.
        payload.pop("entry_policy_rollout", None)
        row.payload = payload
        db.commit()
        return True
