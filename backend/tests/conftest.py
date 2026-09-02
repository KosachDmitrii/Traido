"""Shared test fixtures.

The clock fixture exists because Stage 7 added an RTH gate to the entry path.
Without it the suite would pass on a Tuesday afternoon and fail every weekend,
which is a property of the test harness rather than of the code. It pins "now"
to a real mid-session moment instead of disabling the gate, so the gate itself
still runs on every entry test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")

RTH_INSTANT = datetime(2026, 3, 10, 11, 0, tzinfo=ET)
"""Tuesday 11:00 ET — a regular session, not a holiday or an early close."""


@pytest.fixture(autouse=True)
def isolated_default_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep get_sync_engine() / DbAudit / watch persistence off the real journal.

    LEDGER/OPPORTUNITIES/EXITS are already repointed per-store; anything that
    still calls `get_sync_engine()` (API lifespan, DbAudit, watch CAS) must not
    touch `data/traido_journal.db`.
    """
    from database import session as session_mod
    from database.session import get_sync_engine, init_db

    url = f"sqlite:///{tmp_path / 'unit_journal.db'}"
    monkeypatch.setenv("TRAIDO_JOURNAL_DATABASE_URL", url)
    session_mod.get_sync_engine.cache_clear()
    init_db(get_sync_engine())
    yield
    session_mod.get_sync_engine.cache_clear()


@pytest.fixture(autouse=True)
def frozen_market_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[datetime]:
    """Make execution see a deterministic open market unless a test says otherwise."""
    from trading import execution

    monkeypatch.setattr(execution, "_utcnow", lambda: RTH_INSTANT)
    yield RTH_INSTANT


@pytest.fixture(autouse=True)
def unpaced_market_data(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the vendor quota out of the suite's wall clock.

    The Alpaca adapter paces every request against the account's real quota,
    which is the point in production and pure latency here: the paging test
    alone walks fifty pages, and three requests a second would make it a
    fourteen-second test of arithmetic nobody is asserting.

    Replaced rather than disabled, so the call still goes through `acquire` and
    a test that wants to assert on pacing can install its own bucket.
    """
    from core.concurrency import RateLimiter
    from market_data.providers import alpaca

    # Fresh AccountQuota with a near-unlimited floor bucket — still runs
    # acquire/observe so header wiring stays under test.
    alpaca.reset_account_limiter()
    alpaca.set_account_limiter(RateLimiter(1e6, burst=1e6))
    yield
    alpaca.reset_account_limiter()


@pytest.fixture(autouse=True)
def strict_entry_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin entry aggressiveness to 0 so F3 unit tests see the shipped floors.

    The operator slider persists under data/; without this, a desk session that
    raised aggressiveness would make the suite assert against a different policy.
    """
    from trading import entry_policy

    path = tmp_path / "entry_policy.json"
    monkeypatch.setattr(entry_policy, "POLICY_PATH", path)
    entry_policy.reset_entry_policy_cache()
    entry_policy.set_entry_aggressiveness(0, actor="test")
    yield
    entry_policy.reset_entry_policy_cache()


@pytest.fixture(autouse=True)
def isolated_order_intents(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test its own intent store.

    Unresolved intents are designed to outlive the process and block a symbol
    until reconciliation clears them — correct in production, but it would mean
    one test's UNKNOWN order silently blocks the next test's entry.
    """
    import risk.context_builder  # noqa: F401 — imports the store lazily
    import trading.execution
    import trading.intents
    import trading.reconcile
    from trading.intents import MemoryOrderIntentStore

    store = MemoryOrderIntentStore()
    monkeypatch.setattr(trading.intents, "INTENTS", store)
    monkeypatch.setattr(trading.execution, "INTENTS", store)
    monkeypatch.setattr(trading.reconcile, "INTENTS", store)
    yield


@pytest.fixture(autouse=True)
def stubbed_earnings_calendar(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answer the earnings calendar the way the mock broker answers orders.

    Entries are refused when the calendar cannot be read, and the suite runs
    with no Finnhub key, so without this every execution test would assert
    against `EARNINGS_CALENDAR_NOT_CONFIGURED` instead of the thing it is about.
    The alternative — turning the requirement off for tests — would mean the
    approve path they exercise is not the approve path that ships.

    Stubbing the vendor rather than the gate keeps the whole chain under test:
    the context is still built, the status still travels, the engine still
    decides. Tests that are about an unread calendar ask for
    `unstubbed_earnings_calendar` and get the real, keyless behaviour back.
    """
    from core.enums import EarningsCheck
    from market_data.providers import earnings as earnings_mod

    class _ClearCalendar:
        configured = True

        async def get(self, symbol: str, *, now: datetime | None = None):
            return earnings_mod.EarningsInfo(
                symbol=symbol.upper(),
                status=EarningsCheck.CHECKED,
                note="No earnings scheduled in window",
            )

    # Only the name `context_builder` resolves, never the provider module's own.
    # Patching both would leave the real function unreachable, and the fixture
    # that restores it would restore the stub over itself.
    monkeypatch.setattr("risk.context_builder.get_earnings_calendar", lambda _key: _ClearCalendar())
    yield


@pytest.fixture(autouse=True)
def stubbed_news_feed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The same treatment as the calendar, for the other vendor gate.

    Entries are refused when the headlines could not be read, and the suite runs
    with no Finnhub key, so without this every execution test would assert
    against `NEWS_NOT_CONFIGURED` instead of its subject.

    Stubbed at the vendor, not the gate: the context is still built, the status
    still travels, the engine still decides. Tests about an unread feed call
    `assess_news` directly and get the real, keyless behaviour.
    """
    from core.enums import AssessmentKind, NewsCheck
    from core.schemas import NewsAssessment

    async def _clear(symbol: str, _key=None, **_kw):
        return NewsAssessment(
            kind=AssessmentKind.NEWS,
            symbol=symbol.upper(),
            sentiment="neutral",
            score=50,
            reasons=["stubbed clear feed"],
            status=NewsCheck.CHECKED,
        )

    monkeypatch.setattr("risk.context_builder.assess_news", _clear)
    yield


@pytest.fixture(autouse=True)
def stubbed_sector_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin sector lookup to keyless so a developer's `.env` never reaches Finnhub.

    Curated `universe.json` still classifies names it knows. Names outside the
    file become `SECTOR_NOT_CONFIGURED` rather than a live `profile2` call —
    the suite must not depend on network or on whoever has a key locally.
    Tests about the Finnhub path construct a `SectorResolver` themselves.
    """
    from market_data.providers.sector import SectorResolver

    monkeypatch.setattr(
        "risk.context_builder.get_sector_resolver",
        lambda _key: SectorResolver(api_key=None),
    )
    yield


@pytest.fixture
def keyless_earnings_calendar(
    stubbed_earnings_calendar: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Real calendar code, deliberately without a key — for tests about the gap.

    The key is pinned to None rather than read from settings. Reading it would
    make these tests assert one thing on a machine that has a `.env` key and
    another on a machine that does not, and the machine with a key would have a
    unit test reaching out to Finnhub.

    Depends on the stub by name so it is guaranteed to run after it. Relying on
    autouse ordering instead would make the restore a coin flip.
    """
    from market_data.providers.earnings import EarningsCalendar

    monkeypatch.setattr(
        "risk.context_builder.get_earnings_calendar",
        lambda _key: EarningsCalendar(api_key=None),
    )
    yield


@pytest.fixture(autouse=True)
def isolated_admission_and_watches(
    isolated_default_journal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Admission lives on the journal engine — FKs from opportunities/intents require it.

    Rows are wiped per test so evaluations do not leak across cases. Watches stay
    in-memory (persistence disabled) as before.

    Depends on `isolated_default_journal` so get_sync_engine() never resolves to
    the developer's real journal (stale schema → init_db refuses to boot).
    """
    from sqlalchemy import text

    from database.session import get_sync_engine, session_factory
    from trading import admission_records as adm_mod
    from trading import shadow_outcomes as shadow_mod
    from trading.entry_watches import ENTRY_WATCHES

    engine = get_sync_engine()
    store = adm_mod.AdmissionRecordStore(engine=engine)
    shadow = shadow_mod.ShadowOutcomeStore(engine=engine)
    monkeypatch.setattr(adm_mod, "ADMISSION_RECORDS", store)
    monkeypatch.setattr(shadow_mod, "SHADOW_OUTCOMES", shadow)

    SessionLocal = session_factory(engine)
    with SessionLocal() as session:
        session.execute(text("DELETE FROM admission_records"))
        session.execute(text("DELETE FROM shadow_outcomes"))
        session.commit()

    from trading.entry_watch_persistence import (
        configure_entry_watch_persistence,
        persistence_enabled,
    )

    prev_persistence = persistence_enabled()
    configure_entry_watch_persistence(enabled=False)
    ENTRY_WATCHES.clear()
    yield
    ENTRY_WATCHES.clear()
    configure_entry_watch_persistence(enabled=prev_persistence)
    with SessionLocal() as session:
        session.execute(text("DELETE FROM admission_records"))
        session.execute(text("DELETE FROM shadow_outcomes"))
        session.commit()


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every test's positions out of the developer's journal database.

    `trading.ledger.LEDGER` is a module-level singleton bound to whatever
    `get_sync_engine()` resolves — by default the real SQLite journal in
    `data/`. Any test that reaches the ledger, directly or through
    `ExecutionService`, therefore writes open positions and journal rows into
    the database the desk reads from, and an open position never closed by the
    test stays there afterwards. That is not a cosmetic leak: the desk cannot
    tell a fabricated position from a real one, and shows it as a holding.

    Repointing the singleton's engine rather than replacing the object is what
    makes this reliable. `reconcile`, `api.routes.desk`, `api.routes.review` and
    the position agent all bind `LEDGER` at import time, so swapping the name in
    one module would leave the others still pointed at the real file.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from database.session import init_db
    from trading.ledger import LEDGER

    engine = create_engine(
        "sqlite://",
        future=True,
        # One shared connection, or each session would get its own empty
        # in-memory database and the ledger would appear to forget every write.
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    monkeypatch.setattr(LEDGER, "_engine", engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def isolated_desk_stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The same protection as `isolated_ledger`, for the two stores it missed.

    `EXITS` and `OPPORTUNITIES` are module-level singletons bound to the same
    journal database, and nothing was repointing them — so a test that reached
    either one wrote cards the desk cannot distinguish from real ones.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from database.session import init_db
    from trading.exits import EXITS
    from trading.opportunities import OPPORTUNITIES

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    monkeypatch.setattr(EXITS, "_engine", engine)
    monkeypatch.setattr(OPPORTUNITIES, "_engine", engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def isolated_audit_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep DbAudit JSONL mirrors out of the real audit file."""
    from core import audit as audit_mod

    path = tmp_path / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    orig_init = audit_mod.DbAudit.__init__

    def _init(self, engine=None, *, mirror_jsonl: bool = True):
        orig_init(self, engine, mirror_jsonl=mirror_jsonl)
        if mirror_jsonl:
            self._jsonl_path = path

    monkeypatch.setattr(audit_mod.DbAudit, "__init__", _init)
    yield


@pytest.fixture(autouse=True)
def isolated_external_positions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Per-test memory store for orphan/external incidents."""
    from trading import external_positions as ep
    from trading.external_positions import MemoryExternalPositionStore

    store = MemoryExternalPositionStore()
    monkeypatch.setattr(ep, "EXTERNAL_POSITIONS", store)
    yield


@pytest.fixture(autouse=True)
def admission_metadata_on_store_create(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Attach fail-closed admission metadata at store.create.

    Does not invent R:R, REALISTIC reachability, or lift targets. Only fills
    missing thesis/breakdown/target_model required by the capital path.
    """
    from trading.opportunities import MemoryOpportunityStore, OpportunityStore

    def _wrap(cls):
        orig = cls.create

        def create(self, candidate, risk, mode, *args, **kwargs):
            from tests.support import ensure_admission_ready as _ready

            return orig(self, _ready(candidate), risk, mode, *args, **kwargs)

        monkeypatch.setattr(cls, "create", create)

    _wrap(MemoryOpportunityStore)
    _wrap(OpportunityStore)
    yield


@pytest.fixture(autouse=True)
def cleared_market_for_capital_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Capital-path tests need a real MarketAssessment — missing FRED is not NEUTRAL."""
    from datetime import UTC, datetime

    from core.enums import AssessmentKind, MarketRegimeLabel
    from core.schemas import MarketAssessment

    async def _assess(fred_api_key=None, *, now=None):
        evaluated_at = now or datetime.now(UTC)
        return MarketAssessment(
            kind=AssessmentKind.MARKET,
            regime=MarketRegimeLabel.RISK_ON,
            score=70,
            risk_posture="risk_on",
            reasons=["test_cleared_market"],
            evaluated_at=evaluated_at,
            benchmark="SPY",
            sector_label="technology",
            sector_tradable=True,
        )

    monkeypatch.setattr("agents.market.agent.assess_market", _assess)
    yield
