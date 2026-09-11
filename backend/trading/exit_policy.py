"""Explicit owner-selected Paper exit policy, including existing positions."""

from core.config import get_settings
from core.enums import BrokerEnvironment


def manual_target_exits() -> bool:
    settings = get_settings()
    return (
        settings.broker_env == BrokerEnvironment.PAPER
        and settings.paper_exit_policy == "manual_target"
    )
