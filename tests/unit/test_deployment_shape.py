"""P1-6 / P1-11: the single-worker assumption is checked, not just written down.

Reconciliation is single-flight per process, the opportunity and exit claims are
in-process locks, and without Redis the kill switch is a local file. Each of
those is sound on one worker and unsound on two — and each fails as a duplicate
order, a double approval, or a halt that half took effect, rather than as an
error anyone would see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.deployment import (
    ALLOW_MULTI_WORKER,
    UnsafeDeployment,
    assert_implemented_trading_mode,
    assert_single_worker,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "TRAIDO_API_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(ALLOW_MULTI_WORKER, raising=False)


def test_a_plain_environment_starts() -> None:
    assert_single_worker()


def test_one_worker_stated_explicitly_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assert_single_worker()


@pytest.mark.parametrize(
    "var", ["WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "TRAIDO_API_WORKERS"]
)
def test_more_than_one_worker_refuses_to_start(var: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every variable a supervisor might use, because missing one is missing all of it."""
    monkeypatch.setenv(var, "4")
    with pytest.raises(UnsafeDeployment, match="4 workers"):
        assert_single_worker()


def test_the_refusal_says_which_guarantees_break(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who cannot see the consequence will just raise the worker count."""
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(UnsafeDeployment) as caught:
        assert_single_worker()

    message = str(caught.value)
    assert "protective stops" in message
    assert "kill switch" in message
    assert ALLOW_MULTI_WORKER in message


def test_the_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    monkeypatch.setenv(ALLOW_MULTI_WORKER, "1")
    assert_single_worker()


def test_an_unparseable_worker_count_is_not_read_as_many(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WEB_CONCURRENCY=auto` must not become a refusal to boot at all."""
    monkeypatch.setenv("WEB_CONCURRENCY", "auto")
    assert_single_worker()


def test_the_api_checks_this_before_it_starts_anything() -> None:
    """A check that runs after the scanner is a check that ran too late."""
    import inspect

    from api import main

    source = inspect.getsource(main.lifespan)
    assert "assert_single_worker()" in source, "startup must assert the deployment shape"
    assert source.index("assert_single_worker()") < source.index("start_scanner()"), (
        "the shape must be checked before any agent or loop is started"
    )


class TestTradingModeIsOneTheDeskImplements:
    """Autopilot is an enum member with no behaviour behind it.

    The danger is not that it trades unsupervised — nothing does. It is that an
    operator who believes autopilot is on stops watching, and then reads an
    empty position list as a quiet market rather than as a desk waiting for a
    human who is no longer looking.
    """

    def test_confirmation_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRAIDO_TRADING_MODE", "confirmation")
        assert_implemented_trading_mode()

    def test_autopilot_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRAIDO_TRADING_MODE", "autopilot")
        with pytest.raises(UnsafeDeployment, match="autopilot is not implemented"):
            assert_implemented_trading_mode()

    def test_no_production_code_acts_on_the_autopilot_mode(self) -> None:
        """The refusal is only honest while this stays true.

        If a real autopilot path is ever built, this test fails and points at
        the refusal above, which must be deleted in the same commit.
        """
        allowed = {"core/enums.py", "core/deployment.py"}
        packages = (
            "api",
            "agents",
            "broker",
            "core",
            "database",
            "market_data",
            "quant",
            "risk",
            "trading",
        )

        offenders = []
        for package in packages:
            for path in (REPO / package).rglob("*.py"):
                relative = str(path.relative_to(REPO))
                if relative in allowed:
                    continue
                if "autopilot" in path.read_text().lower():
                    offenders.append(relative)
        assert not offenders, (
            f"autopilot is now referenced by {offenders} — if it does something, "
            "remove the startup refusal in core/deployment.py"
        )

    @pytest.fixture(autouse=True)
    def uncached_settings(self):
        """`get_settings` is cached, so the env var alone would not be read."""
        from core.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()
