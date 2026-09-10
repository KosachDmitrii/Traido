"""Frozen session selection with an audited, one-time unpublished limit upgrade."""

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


def create_session(day: str, payload: dict[str, Any]) -> dict[str, Any]:
    with session_factory()() as db:
        row = db.get(OrbSessionRow, day)
        if row:
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
    """Apply the user-authorized Paper rollout only before any publication.

    Publication takes the same row lock and compares the complete plan, so a
    candidate evaluated against an older limit cannot commit after this change.
    Existing proposals, positions, triggers and stops are never rewritten.
    """
    from decimal import ROUND_FLOOR, Decimal

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
            if state.get("opportunity_id"):
                continue
            plan = OrbPlan.model_validate(raw)
            if now >= plan.entry_deadline:
                continue
            previous_parameters = plan.evidence.get("parameters", {})
            if previous_parameters.get("entry_policy_revision") == revision:
                continue
            old_limit = (plan.trigger + (plan.trigger - plan.stop) * Decimal("0.25")).quantize(
                Decimal("0.01"), rounding=ROUND_FLOOR
            )
            if plan.max_entry != old_limit:
                continue
            new_limit = (
                plan.trigger
                + (plan.trigger - plan.stop) * Decimal(str(PARAMETERS["max_entry_drift_r"]))
            ).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
            evidence = deepcopy(plan.evidence)
            change = {
                "symbol": symbol,
                "revision": revision,
                "at": now.isoformat(),
                "old_max_entry": str(plan.max_entry),
                "new_max_entry": str(new_limit),
                "previous_parameters": previous_parameters,
                "old_version": plan.version,
                "new_version": VERSION,
                "reason": "USER_REQUESTED_PAPER_ENTRY_SIMPLIFICATION",
            }
            evidence["entry_policy_change"] = change
            evidence["parameters"] = deepcopy(PARAMETERS)
            updated = {**raw, "version": VERSION, "max_entry": str(new_limit), "evidence": evidence}
            payload["plans"][symbol] = OrbPlan.model_validate(updated).model_dump(mode="json")
            revisions.append(change)
        payload["version"] = VERSION
        payload["entry_policy_rollout"] = revision
        payload["entry_policy_changes"] = revisions
        payload["parameters"] = deepcopy(PARAMETERS)
        row.payload = payload
        db.commit()
        return payload
