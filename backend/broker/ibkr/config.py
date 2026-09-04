"""
IBKR connection configuration.

The single most expensive configuration mistake available in this codebase is
pointing a paper-labelled deployment at a live port. TWS and IB Gateway use
fixed, well-known ports per environment, so the mismatch is detectable — and
this module treats it as fatal rather than as a warning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.enums import BrokerEnvironment

PAPER_PORTS: frozenset[int] = frozenset({7497, 4002})
"""TWS paper (7497) and IB Gateway paper (4002)."""

LIVE_PORTS: frozenset[int] = frozenset({7496, 4001})
"""TWS live (7496) and IB Gateway live (4001)."""


class IBKRConfigError(RuntimeError):
    """Configuration that could route real money somewhere unintended."""


@dataclass(frozen=True)
class IBKRTransportConfig:
    """Everything needed to open an IB session, and nothing secret.

    IB authenticates through the running TWS/Gateway session rather than
    through an API key, so there is no credential here to leak into a log.
    """

    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    account: str | None = None
    environment: BrokerEnvironment = BrokerEnvironment.PAPER
    connect_timeout_sec: float = 10.0
    reconnect_backoff_sec: float = 5.0
    max_reconnect_attempts: int = 3

    def __post_init__(self) -> None:
        if self.environment is BrokerEnvironment.PAPER and self.port in LIVE_PORTS:
            raise IBKRConfigError(
                f"environment is PAPER but port {self.port} is a live IB port. "
                "Refusing to connect: this is the mistake that trades real money."
            )
        if self.environment is BrokerEnvironment.LIVE and self.port in PAPER_PORTS:
            raise IBKRConfigError(f"environment is LIVE but port {self.port} is a paper IB port.")
        if self.client_id < 0:
            raise IBKRConfigError("client_id must be non-negative")

    @property
    def is_paper(self) -> bool:
        return self.environment is BrokerEnvironment.PAPER

    def describe(self) -> str:
        """One line for startup logs. Contains no secrets by construction."""
        return (
            f"BROKER=IBKR ENVIRONMENT={self.environment.value.upper()} "
            f"HOST={self.host} PORT={self.port} CLIENT_ID={self.client_id} "
            f"ACCOUNT={self.account or 'default'}"
        )

    @classmethod
    def from_env(cls) -> IBKRTransportConfig:
        """Read configuration without ever defaulting to live.

        An unset or unrecognised environment resolves to PAPER. The safe value
        is the one you get by forgetting to set anything.

        IBKR vars are not on ``Settings``; load ``.env`` so Gateway port/account
        from the repo root reach ``os.environ`` (same file pydantic uses).
        """
        if not os.getenv("TRAIDO_IBKR_PORT") and not os.getenv("TRAIDO_IBKR_ACCOUNT"):
            from core.config import _ENV_FILE

            if _ENV_FILE.is_file():
                from dotenv import load_dotenv

                load_dotenv(_ENV_FILE, override=False)
        raw_env = (os.getenv("TRAIDO_IBKR_ENV") or "paper").strip().lower()
        environment = BrokerEnvironment.LIVE if raw_env == "live" else BrokerEnvironment.PAPER
        default_port = 7497 if environment is BrokerEnvironment.PAPER else 7496
        return cls(
            host=os.getenv("TRAIDO_IBKR_HOST", "127.0.0.1"),
            port=int(os.getenv("TRAIDO_IBKR_PORT", str(default_port))),
            client_id=int(os.getenv("TRAIDO_IBKR_CLIENT_ID", "1")),
            account=os.getenv("TRAIDO_IBKR_ACCOUNT") or None,
            environment=environment,
        )
