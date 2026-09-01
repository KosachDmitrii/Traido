"""What the desk assumes about the shape of its own deployment.

Several safety claims in this system are *process-wide*, not cluster-wide:

- reconciliation is single-flight through one in-process supervisor, so two
  replicas can run two passes over one position and place two stops;
- the opportunity and exit claims are `threading.Lock` plus a status read, which
  two processes pass simultaneously;
- the kill switch falls back to a local file when Redis is absent, so one
  replica can be halted while another keeps trading.

Each of those is fine on one worker and unsound on two, and none of them fails
loudly when the assumption breaks — they fail as a duplicate order, a double
approval, or a halt that only half took effect. That is the worst failure shape
available: silent, occasional, and only visible in the money.

So the assumption is checked rather than documented. The check is by
configuration rather than by counting siblings, because a process genuinely
cannot see its peers — but the configuration is what puts them there, and every
supervisor in use announces it in the environment.
"""

from __future__ import annotations

import os

_WORKER_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "TRAIDO_API_WORKERS")
"""Every variable a supervisor might use to ask for more than one worker.

`WEB_CONCURRENCY` is honoured by uvicorn and gunicorn both, and by most PaaS
runtimes; the rest are the explicit forms.
"""

ALLOW_MULTI_WORKER = "TRAIDO_ALLOW_MULTI_WORKER"
"""The deliberate override, for whoever has actually made the claims cluster-safe.

Named for what it permits rather than what it disables, so that finding it set
in an environment file is a question rather than a shrug.
"""


class UnsafeDeployment(RuntimeError):
    """The desk was asked to run in a shape its safety claims do not survive."""


def requested_workers() -> int:
    """How many API workers the environment asks for. One unless told otherwise."""
    most = 1
    for var in _WORKER_VARS:
        raw = os.getenv(var)
        if not raw:
            continue
        try:
            asked = int(raw)
        except ValueError:
            continue
        most = max(most, asked)
    return most


def assert_single_worker() -> None:
    """Refuse to start multi-worker, unless someone has said they mean it.

    Refusing at boot rather than warning: a warning about a race is read after
    the race, and the whole point of the checks it protects is that they are the
    difference between one broker order and two.
    """
    if os.getenv(ALLOW_MULTI_WORKER, "").strip().lower() in {"1", "true", "yes"}:
        return

    asked = requested_workers()
    if asked <= 1:
        return

    named = ", ".join(f"{v}={os.getenv(v)}" for v in _WORKER_VARS if os.getenv(v))
    raise UnsafeDeployment(
        f"The API is configured for {asked} workers ({named}), and several of this "
        "desk's safety guarantees hold only within one process:\n"
        "  - reconciliation is single-flight per process, so two workers can place "
        "two protective stops for one position;\n"
        "  - opportunity and exit claims are in-process locks, so two workers can "
        "approve the same card;\n"
        "  - without REDIS_URL the kill switch is a local file, so halting one "
        "worker does not halt the others.\n"
        "Run one worker, or set "
        f"{ALLOW_MULTI_WORKER}=1 if those claims have been made cluster-safe."
    )


def assert_implemented_trading_mode() -> None:
    """Refuse to start in a mode the desk does not actually implement.

    `TradingMode.AUTOPILOT` is an enum member and nothing else: no code path
    approves an opportunity without a human, so setting it changes only the
    label written onto each card. That is a safe *behaviour* — the desk keeps
    asking — but a dangerous *belief*, because an operator who thinks autopilot
    is on stops watching a desk that has quietly stopped trading, and reads the
    empty position list as "no setups" rather than "nobody pressed approve".

    Config that silently does nothing is the failure this refusal exists to
    prevent. When autopilot is built, delete this check in the same commit.
    """
    from core.config import get_settings
    from core.enums import TradingMode

    mode = get_settings().trading_mode
    if mode is not TradingMode.AUTOPILOT:
        return

    raise UnsafeDeployment(
        "TRAIDO_TRADING_MODE=autopilot, but autopilot is not implemented: every "
        "opportunity still waits for a human to approve it. Running in this mode "
        "would label the cards as autonomous while nothing is ever executed "
        "unattended.\n"
        "Set TRAIDO_TRADING_MODE=confirmation, which is what the desk does."
    )
