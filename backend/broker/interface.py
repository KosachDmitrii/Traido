"""Broker package — paper first. Live adapter must not be imported by default."""

from core.enums import BrokerConnectionState, BrokerEnvironment


class BrokerError(RuntimeError):
    """Base for broker failures. Subclasses RuntimeError so existing callers still catch it."""


class BrokerRejection(BrokerError):
    """The broker answered, and the answer was no.

    Safe to treat as "no order exists": the broker processed the request and
    declined it, so a retry cannot produce a duplicate.
    """


class BrokerUnreachable(BrokerError):
    """We never got an answer.

    The order may or may not be live. This must never be downgraded to a
    rejection — that is exactly how duplicate positions get created.
    """


def broker_connection_state(broker: object) -> BrokerConnectionState:
    """How healthy the link to this broker is.

    An adapter that does not report is READY by construction: a stateless REST
    client has no session to lose, and a broken call surfaces as
    `BrokerUnreachable` rather than as a quietly degraded state.
    """
    getter = getattr(broker, "connection_state", None)
    if getter is None:
        return BrokerConnectionState.READY
    try:
        state = getter()
    except Exception:  # noqa: BLE001 — an adapter that cannot answer is not connected
        return BrokerConnectionState.DISCONNECTED
    return state if isinstance(state, BrokerConnectionState) else BrokerConnectionState.DEGRADED


def assert_paper_only(environment: str) -> None:
    if environment != BrokerEnvironment.PAPER:
        raise RuntimeError(
            "Traido V1 refuses non-paper broker environments. "
            "Live trading is disabled until Stage 7 gate."
        )


def resolve_broker_identity(broker: object) -> tuple[str, str, str]:
    """Return (broker_name, account_id, environment). Fail closed if unknown."""
    name = type(broker).__name__
    env: object = getattr(broker, "environment", None)
    if callable(env):
        env = env()
    if env is not None and hasattr(env, "value"):
        env = env.value
    env_s = str(env or "").strip().lower()
    if env_s != BrokerEnvironment.PAPER.value:
        raise RuntimeError("BROKER_ENVIRONMENT_BLOCKED")
    account_id: object = getattr(broker, "account_id", None)
    if callable(account_id):
        account_id = account_id()
    if not account_id:
        account_id = f"{name}:paper"
    return name, str(account_id), env_s

