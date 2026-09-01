# Gap register

Derived from `runtime-path-audit.md`. Every entry cites the audit section that
establishes it. Severity follows the programme's definition:

- **P0** — capital safety, duplicate order, broker truth, unprotected exposure
- **P1** — production correctness, bypassable gate, reconciliation, stale data
- **P2** — observability, operational reliability, performance
- **P3** — architecture cleanup, maintainability

One standing caveat: `core/config.py:49` refuses to start against a non-paper
broker, so no P0 below is currently endangering real money. They are P0 because
they would be the moment that block is lifted, and because the promotion order
requires them closed before IBKR Paper certification, not after.

---

## Status

Every P0 and every P1 that gates IBKR Paper is closed. Each closure below is
carried by a test that was red against the code as it stood — the entries keep
their original text so the fix can be read against the defect it answers.

| # | Status | Proof |
| --- | --- | --- |
| P0-1 | **Closed** | `test_24`, `test_24a` — two passes, and a lost reply, each leave one stop |
| P0-2 | **Closed** | `tests/integration/test_background_reconciliation.py` — passes accrue with no HTTP traffic |
| P0-3 | **Closed** | `tests/unit/test_exit_atomicity.py` — a crash between the writes loses a reduction rather than duplicating one |
| P0-4 | **Closed** | `tests/integration/test_single_flight_reconciliation.py` — two overlapping GETs, one pass |
| P0-5 | **Closed** | `test_8_stale_reconciliation_refuses_the_entry` |
| P0-6 | **Closed** | `assert_protection_never_exceeds_holdings`, asserted across the reconciliation suite |
| P1-1 | **Closed** | `test_broker_mutations_are_confined_to_the_broker_layer` now covers `cancel_order` |
| P1-2 | **Closed** | Protective placement carries a durable `PROTECTIVE_EXIT` intent |
| P1-3 | **Closed** | `test_a_stray_protective_sell_beyond_the_position_is_cancelled` |
| P1-4 | **Closed** | `check_instrument_eligibility`, wired into `_entry_gates` |
| P1-5 | **Closed** | `check_bar_freshness`, wired into `_entry_gates` |
| P1-7 | **Closed** | `tests/integration/` — 73 tests through the real HTTP boundary |
| P1-9 | **Closed** | `tests/unit/test_scan_context.py` — one broker per cycle, not per symbol |
| P1-12 | **Closed** | Partial unique index, migration `0006`; `tests/unit/test_one_open_position_per_symbol.py` |
| P1-6, P1-11 | **Contained** | `assert_single_worker` + DB CAS claim (`WHERE status` + `FOR UPDATE`) + intent `transition_from(CREATED→SUBMITTING)`; stress: `test_concurrent_approval_stress.py`. Multi-node still needs distributed lease. |
| P1-8 | **Partial** | `trading/decision_pipeline.py` declares `NEW_EXPOSURE_GATE_ORDER`; execution imports it; full gate runner not yet the sole body of `decide()` |
| P1-10 | **Partial** | `proposed_qty` / `approved_qty` / `executed_qty` on `TradeOpportunity`; set at create and EXECUTED |
| P0-7 | **Closed** | `tests/integration/test_kill_switch_protects_what_is_open.py` |
| P0-8 | **Closed** | Header auth + `core.redaction`; `tests/unit/test_secrets_never_reach_logs.py` |
| P1-13 | **Closed** | `assert_implemented_trading_mode`; `tests/unit/test_deployment_shape.py` |
| P1-14 | **Closed** | `tests/unit/test_alpaca_bars_are_paged.py` — the adapter followed `next_page_token`, having silently served the oldest page of every intraday window |
| P1-15 | **Closed** | `tests/unit/test_strategy_refuses_stale_bars.py` — `check_bar_freshness` now runs on every timeframe in `Supervisor._load_features`, where the decision is drawn, and a stale one fails the scan instead of being skipped. It had only ever guarded the daily series the liquidity gate reads, which is why P1-14 survived a gate written for exactly it |
| P1-14 | **Closed** | `NewsCheck` + `require_news_check`; `tests/unit/test_news_fail_closed.py` |
| P1-15 | **Closed** | Separate `FAILURE_TTL` + `core.vendor_http`; `tests/unit/test_vendor_retry.py` |
| P1-16 | **Closed** | `tests/unit/test_exit_signal_is_an_event.py` — the exit rule compared two levels and called it a cross, on a timeframe the entry had not used |
| P1-17 | **Closed** | `isolated_desk_stores` in `tests/conftest.py` — `EXITS` and `OPPORTUNITIES` wrote test rows into the desk's own journal |
| P1-18 | **Closed** | `tests/unit/test_repricing_preserves_the_setup.py` — repricing turned a 2:1 card into a 0.32:1 order, and the ledger recorded 2.0 |
| P0-9 | **Closed** | `tests/contract/test_kill_switch_scope_is_the_same_everywhere.py` — P0-7 was fixed in one adapter of three, and the mock was one of the two that stayed broken |
| P1-19 | **Closed** | `tests/integration/test_background_exit_assessment.py` — the exit assessment ran on a page render |
| P1-20 | **Closed** | `tests/unit/test_operator_can_close_a_position.py` — the desk could open a position it had no way to close |
| P1-21 | **Closed** | `tests/unit/test_schema_is_prepared_once.py` — every store operation re-inspected the whole schema, and two doing it at once corrupted each other's reflection |
| P1-22 | **Closed** | `tests/unit/test_stale_proposals_are_taken_down.py` — nothing ever retracted a BUY card, so dead proposals held the queue slots that stop the scanner |
| P2, P3 | **Open** | Metrics, alerting and decomposition, in that order |

Note: `P1-14` and `P1-15` each name two different findings above — the bar-paging
pair and the vendor-read pair, added by separate sweeps. Left as they are because
both are cited by number elsewhere; new findings continue from `P1-16`.

Two further findings came out of the readiness sweep rather than the original
audit, so they are numbered after it.

**P0-7 — the kill switch disarmed the desk it was meant to protect.** The
refusal lived in `AlpacaBroker.place_order`, the one layer that cannot tell an
entry from a protective stop, so halting the desk also refused the stop that
reconciliation was trying to install and the emergency close that is the last
way out of an unprotectable position. The switch is pressed when something has
already gone wrong, which is exactly when open positions most need defending, so
this inverted its purpose in the worst available circumstances. Fixed by
carrying `IntentPurpose` down to the broker on `OrderRequest`, defaulting to
`ENTRY` so an unlabelled order is still refused.

**P0-8 — the Finnhub API key was printed to the desk and written to the audit
table.** Finnhub authenticates by query parameter, so `httpx` put the key in the
request URL and `HTTPStatusError` put the URL in its message; the supervisor
stringified that exception into the activity board, the agent status line, the
pipeline result and a durable `ScanJobFailed` row. Every Finnhub 503 — frequent
on the free tier — leaked a live credential onto the screen and into the
database. Fixed at the root by sending `X-Finnhub-Token` as a header in both
Finnhub callers, and netted by `core.redaction`, applied inside `BOARD.log`,
`BOARD.set_agent` and both audit sinks so that no future call site has to
remember. `tests/unit/test_secrets_never_reach_logs.py`.

**P1-14 — a missing news key was read as "no bad news".** `propose_trade` vetoes
on `news.sentiment == "negative"`, so news is a gate, not decoration. With no
Finnhub key the agent returned a neutral 50 and the veto silently could not
fire — the same fail-open shape as the earnings calendar before it was closed.
A vendor outage did the opposite and raised, taking the whole symbol's pipeline
down; the scanner filed that as `no_candidate`, indistinguishable from "there
was no setup here".

Both paths now report a `NewsCheck` status and the risk engine refuses on it by
name, mirroring the calendar exactly: `NEWS_NOT_CONFIGURED` for a missing key,
`NEWS_UNAVAILABLE` for an outage, `NEWS_UNVERIFIED` for a caller who never
asked. `require_news_check=false` is the only way past and is recorded in
`limits_applied`. The scanner passes the status it already read; approval
re-reads, because a card can wait an hour and a headline can break in it.
`tests/unit/test_news_fail_closed.py`.

**P1-15 — one 503 removed a symbol from the universe for six hours.** Closing
P1-14 made vendor reads fail closed, which raised the price of a dropped
request: "could not read the calendar" now refuses the entry outright. The
calendar cached the answer without inspecting it, under a single six-hour
`CACHE_TTL` chosen for the fact that earnings dates do not move. Failures do
move — they last seconds — so a symbol whose read hiccuped once was refused
every cycle until evening, long after Finnhub was answering again. Observed
live: nineteen of twenty-three risk rejections in one session were
`EARNINGS_CALENDAR_UNAVAILABLE`, from an outage that had ended. News, which
caches nothing, recovered on its own and showed the asymmetry had no
justification.

Three changes, all in the same shape. Failures cache under their own
`FAILURE_TTL` of two minutes — short enough that the next cycle retries, long
enough that a burst of lookups for one symbol is not a burst of retries against
a struggling vendor. Both callers retry through `core.vendor_http`: three
attempts, backoff doubling from 0.4s, on 5xx, 429 and transport errors only,
since a 401 answers the same way twice. And failures are named by status code,
because `Earnings lookup failed: HTTPStatusError` cannot distinguish an outage
to wait out from a key to replace — the reason diagnosing this one needed the
audit table instead of the log line. Naming by code was previously avoided to
keep the vendor's URL-bearing message out of the log; with redaction now at the
sinks, the code alone is both safe and sufficient. `tests/unit/test_vendor_retry.py`.

**P1-13 — `TRAIDO_TRADING_MODE=autopilot` was a silent no-op.** `AUTOPILOT` is
an enum member with no behaviour anywhere in the runtime, so selecting it
labelled each card as autonomous and changed nothing. The behaviour was safe —
the desk kept asking for a human — but the belief was not: an operator who
thinks autopilot is running stops watching, and reads an empty position list as
a quiet market rather than as a desk waiting for someone who has left. The API
now refuses to boot in that mode, and a test fails if any production module ever
starts acting on it, so the refusal cannot outlive the gap it describes.

**P1-16 — the desk proposed selling a position eighteen seconds after buying
it.** MO filled at 68.28 on 2026-08-31 and the position agent immediately raised
a sell card reading "SMA20 crossed below EMA50 while in profit". Nothing had
crossed. Three defects stacked:

The rule compared the two averages on the newest bar only and reported the
result as a crossing. A level is not an event — MO's daily SMA20 sat 3.3% below
its EMA50 and had for weeks — so the card re-appeared on every pass and told the
operator that something had just happened each time. `_crossed_below` now needs
the previous bar to have been at or above, and an unreadable bar is not a cross.

"In profit" meant a single tick. The position was up 0.2%, less than the ten
basis points paid to cross the spread on the way in and the ten it would pay on
the way out, so acting on the card would have realised a loss. The floor is now
`round_trip_cost_pct()`, derived from the same `ENTRY_BUFFER_BPS` the entry pays
— moved to `trading.pricing` so there is one number rather than two that drift.

And the two agents were reading different series. The entry drew its geometry
from the intraday snapshot `_confluence` selects, while the exit judged the
daily one; MO's entry of 68.288 was an intraday SMA20 and the exit compared it
against a daily SMA20 of 66.85. The condition was therefore already true when
the position was opened, which is the only reason it could fire in eighteen
seconds. `TradeCandidate.exec_timeframe` now travels with the geometry it
describes, through the ledger payload to the position agent.

Two further gaps surfaced while fixing it. The position agent had no freshness
check at all, so an exit could be judged on a series that had stopped updating —
it now refuses to propose, which is safe here in a way it would not be on the
entry path, because the protective stop is resting at the broker regardless. And
a proposal was never withdrawn: the MO card would have stayed on the desk
indefinitely after the rule stopped believing it. Withdrawal runs through
`claim`, so a card already being acted on is refused rather than pulled out from
under the operator, and a symbol skipped for stale data is left alone so that a
vendor outage cannot quietly clear the board.

**P1-17 — the test suite wrote into the desk's journal.** `isolated_ledger`
already existed for exactly this reason, and covered one of the three
module-level stores bound to the journal engine. `EXITS` and `OPPORTUNITIES`
were not repointed, so any test touching them wrote rows the desk cannot
distinguish from real ones. This is the origin of the 83 AAPL positions, 180
AAPL opportunities and 109 journal rows priced at 100.00 that the Review panel
reports to the operator as an 86-trade history with a 0% win rate — and it
reaches conclusions from them, advising "review entry timing" on synthetic data
where entry equals exit. Exit cards leaked in every status the machine has,
including one left `approving`: a fabricated symbol part-way through the sell
path. Closed by `isolated_desk_stores`. The residue itself is untouched and
still on the board; clearing it is a separate decision.

**P1-18 — approval could buy a different trade from the one on the card.** The
entry is repriced to the live offer at approval; the stop and the target are
not. Every cent paid above the card therefore lengthens the risk and shortens
the reward at the same time, and nothing measured the result. OXY was drawn at
59.1072 against a stop at 58.4328 — 0.67 of risk — and filled at 59.97, having
paid 0.86 to get in: more than the entire distance it was risking. A 2:1 setup
reached the broker as 0.32:1, $128 of risk buying $40 of reward, and the card on
screen still read 2.0. `PRICE_MOVED_PAST_SETUP` allowed it because the price had
not passed the target, only most of the way to it. Three of the four positions
opened that afternoon were taken below the strategy's own bar: 1.97, 1.53, 0.32.

The bound is on the entry rather than on the resulting ratio, and that choice is
the substance of the fix. The obvious re-check — demand 2:1 after repricing —
refuses *every* entry, because the strategy builds its target at exactly two
times risk, so a card reads 2.0 and never more. The first version of this fix
did exactly that and would have stopped the desk trading altogether. What
survives is `MAX_ENTRY_SLIPPAGE_R`: at most a quarter of the planned risk may be
spent getting in, which is the same statement the doctrine wanted — past that
line it is no longer the setup that was analysed, because a pullback entry
bought a quarter of its own risk above the pullback is not one — and which puts
the worst admissible trade at 1.4:1 by arithmetic rather than by a second
constant.

Two smaller things fell out. The spread check now runs before pricing rather
than only after, because a book too wide to trade is not a book to price
against, and a 400bps spread was being reported as an entry that had drifted
from its card: true, and useless. And the ledger recorded the card's
`risk_reward` rather than the one it got — all four positions stored 2.0 — so
the journal the strategy is later judged by described trades that never
happened. It now measures from the fill.

**P0-9 — P0-7 was closed in one broker adapter out of three.** The scoped
refusal (`is_kill_switch_on() and not request.reduces_risk`) was written into
`AlpacaPaperBroker` and nowhere else. `IBKRBroker` and `MockPaperBroker` still
refused every order while halted, so the guarantee held for the broker in use
and not for the broker in the vendor lock: on IBKR, pressing the switch would
still have refused the protective stop reconciliation was installing and the
emergency close that is the last way out — the original defect, waiting on the
promotion path.

The mock being wrong is why nothing caught it, and that is the part worth
keeping. A test asserting "an exit is placed while halted" could not pass
against the mock, so the property was never written down at all, and the fix
stayed wherever it had first been applied. The new test is parametrised over the
adapters and asserts on the refusal as written, because the defect was never
that the rule was wrong; it was that the rule lived in one place and the
question gets asked in three.

**P1-19 — the exit assessment ran on a page render.** Reconciliation was moved
off `GET /api/v1/desk/broker` under P0-2 and the position agent was left behind,
so the two halves of watching a position — is it still protected, and should it
still be held — ran on different schedules. The cost is quieter than a duplicate
stop but not smaller: a proposal that should have been raised at 15:40 is raised
whenever somebody next looks, and a proposal that should have been *withdrawn*
stays on the board until then. The second is the worse one, since the operator
returns to a sell card whose reason stopped holding hours ago with no way to
tell. Now a loop of its own in `agents/position/loop.py`, deliberately separate
from reconciliation: reconciliation must run when market data is unreadable and
this refuses when it is, and folding them together would let a quote-feed outage
delay a protective stop.

**P1-20 — the desk could open a position it had no way to close.** Every exit
required an agent to have raised a card first, which made the only sell button
on the desk a side effect of a judgement the agent might never make. Correcting
the exit rule under P1-16 removed the last standing card and left four open
positions with no way to act on any of them. Protection still bounded the loss
and the broker's own terminal still worked, but a desk that cannot close what it
opened is not a desk. `close_position` synthesises the proposal `decide_exit`
already knows how to carry out rather than opening a second route to the broker,
so the claim machine, the durable intent, the sizing from broker truth and the
ledger reduction are the ones already under test — and a second click meets the
same refusal it would meet on a card.

**P1-21 — every database operation re-checked the whole schema, and two at once
broke it.** Seven modules built their session factory the same way: `eng =
init_db(engine or get_sync_engine())`, inline, on each call. `init_db` is a
startup assertion — `create_all`, an index sweep, then `get_columns` for every
table in the metadata — so it ran in full on every audit append, every intent
read and every ledger query, dozens of PRAGMA round trips deep in the path an
order is placed through.

Found by pulling on a flake rather than by reading the code, which is the part
worth recording. `test_22` fires two approvals at one opportunity and asserts
one order results; it failed about one run in ten under randomised ordering,
and only under randomised ordering, because whether the schema had already been
reflected depended on which tests had run first. The traceback ended in
`IndexError: tuple index out of range` inside SQLAlchemy's SQLite dialect, which
reads as a corrupt database rather than as what it was: reflection consumes
PRAGMA cursors, and two threads inspecting one table over one connection tear
each other's results apart. The intermittency was the finding. A test that fails
one run in ten is normally filed as flaky and re-run.

Also wrong beyond the cost and the race: re-asking a startup assertion mid-flight
means a store call can raise a migration error from inside order placement,
where nothing is prepared to interpret one. `database.session.session_factory`
now prepares each engine once behind a lock, remembering the engine weakly and
*not* the factory built from it — a `sessionmaker` holds its own engine, so
caching factories under weak keys would have every value keep its key alive.

**P1-22 — nothing ever took a BUY card back down.** Entry proposals were
written and never retracted. The hour-long TTL existed but was read only inside
`decide`, so an expired card kept its buttons until somebody pressed them and
was told it had expired; and one-position-per-symbol was enforced only at the
click, so a symbol that gained a position kept offering an entry that
`POSITION_ALREADY_OPEN` was certain to refuse. Exit cards had been taught to
withdraw themselves a day earlier; entry cards never had been.

Found by looking, but the desk was already in the failure state: three cards
standing — MDLZ, MO, KO — against four open positions, every one of them
unclickable. The consequence is not a bad trade, since the click re-checks
everything; it is that the queue holds five, and at five
`agents/scanner/agent.py` stops scanning the universe entirely. Three of the
five slots were held by proposals that could never become trades, so dead cards
were crowding out live ones and the desk's own throttle was being spent on
nothing.

Three changes. `trading.opportunities.withdraw_unactionable` sweeps the queue
from the scanner cycle (before the slots are counted, so a freed slot is usable
in that same cycle) and from the reconciliation pass (every thirty seconds,
which is the rate the screen actually refreshes). `run_symbol_pipeline` asks
the ledger before it measures anything, so a held symbol never reaches the
supervisor — the analysis was the expensive half, and it was being spent on
cards that could not be acted on. And the card now shows its own age, which is
what would have made the stale prices visible without reading the database.

The sweep is deliberately narrow, and that is the part worth recording. Only
time elapsed and a position now held retract a card. Spread, session, quote age
and price movement all refuse an entry too, and all of them come back within
minutes — sweeping on those would delete a good setup for being briefly
unbuyable, which is a worse failure than the one being fixed. Every transition
goes through `claim`, so a card the operator has already pressed is `APPROVING`
by then and wins the race by construction.

What "contained" means for P1-6 and P1-11, since it is not the same as closed.
Those two are one fact from two directions: several claims here hold per
process — single-flight reconciliation, the claim locks, the file-backed kill
switch — so the desk must run as one worker. The containment is that it now
*refuses to boot* as anything else, naming which guarantees would break, instead
of running happily and losing them. Making the claims genuinely cluster-safe
(row locks, a Redis-backed reconciliation lease) is still open work; until then
the deployment shape is an enforced invariant rather than a convention in a
document nobody reads at three in the morning.

---

## P0 — capital safety

### P0-1 · Two concurrent reconciliation passes place two protective stops

Audit §5.1, §6.4. `reconcile_protective_orders` reads the open-order book,
finds the recorded stop absent and places a replacement with no durable intent,
no idempotency key and no lock. The client id is `traido-s-recover-{uuid4()}`
(`execution.py:1038`), so the broker cannot deduplicate it either — unlike the
entry path, whose client id is derived from the intent.

`ledger.set_stop_order_id` keeps only the last id, so the other stop becomes an
orphan resting SELL for the full position size. When the position is exited it
can execute against shares that no longer exist and open a short, in a system
whose policy disables shorting.

No sweep detects it afterwards: the loop asks only whether the stop it recorded
still exists, never whether the broker holds SELL orders it does not know about.

**Fix shape.** Give protective placement a durable intent
(`IntentPurpose.PROTECTIVE_EXIT`, already declared and unused) keyed
`protection:{position_id}:{generation}`, derive the client id from it, and add
an orphan-SELL sweep to reconciliation.

### P0-2 · Reconciliation is not a control loop

Audit §5.2. `api/main.py:81` starts one background task, the scanner.
Reconciliation lives inside the handler for `GET /api/v1/desk/broker`. With the
dashboard closed, protective orders are unverified, orphans undetected and
`UNKNOWN` intents unresolved for as long as that lasts. Overnight that is the
normal state.

**Fix shape.** A supervised background loop owning reconciliation, with the
route reading its last result rather than driving it. Must land *after* P0-4,
see the ordering note below.

### P0-3 · A partial exit is applied in two separate transactions

Audit §6.3. `apply_exit_to_ledger` (`trading/intents.py:273`) reduces the
position in one committed transaction and records `applied_exit_qty` in
another. A crash or a second reconciliation pass between them reduces the
position twice for one fill, or loses the bookkeeping that makes the operation
idempotent. This is the precise mechanism the partial-exit design depends on.

**Fix shape.** One transaction spanning both writes, or an idempotency guard
that is itself the write (conditional update on `applied_exit_qty`).

### P0-4 · A GET request mutates the broker, unserialised

Audit §2.1. `GET /api/v1/desk/broker` places stops, cancels and re-places
stops, emergency-closes positions and cancels resting entry orders.
`?fresh=true` bypasses both the response cache and the 30-second interval, and
nothing serialises concurrent callers. Browser prefetch, a retried request, an
uptime probe or two open dashboard tabs all trigger it.

This is the delivery mechanism for P0-1. It is listed separately because it
must be closed first.

**Fix shape.** Reconciliation becomes a background loop (P0-2) that the route
observes; a single-flight guard so overlapping passes cannot exist.

### P0-5 · Nothing refuses to trade on stale broker truth

Audit §5.3. Reconciliation age is computed and rendered (`desk.py:339`) but no
gate reads it. An entry can be approved when the last successful reconciliation
is arbitrarily old, including never — `_LAST_RECONCILE_WALL` starts as `None`.

**Fix shape.** A `RECONCILIATION_STALE` gate on new exposure only, with the
threshold in config. Must land *after* P0-2, or it blocks every entry.

### P0-6 · Protection is sized from the book, including when the book is known wrong

Found by `tests/integration/test_reconciliation_and_races_end_to_end.py::test_21b`,
not by code reading.

`reconcile_position_quantities` (`trading/reconcile.py:411`) handles a venue-side
shrink well: local 50 against a broker 25 with no exit fills to explain it is
refused rather than absorbed — the symbol is blocked as `UNKNOWN` and
`PositionQuantityMismatch` is audited. That is the right call.

`reconcile_protective_orders` then runs in the same pass and compares the
resting stop against `row.qty` — the local number that has just been proved
wrong. It sees a stop for 50 against a book of 50, concludes the protection is
correctly sized, and moves on. The venue holds 25 shares with a resting SELL for
50 above them.

The resize path exists and its own comment names this as the dangerous
direction: "Too large … would sell shares we no longer own". It is simply never
reached, because the comparison is against the book rather than against broker
truth.

**Fix shape.** Size protection from the broker's reported position, not the
ledger row; when the two disagree, resize down to the smaller of the two before
anything else.

### Corroboration from Phase 1: why the fix shape for P0-1 is the right one

`test_23` and `test_24` run the same race, through the same route, against the
same reconciliation pass, with the interleaving pinned by a barrier at the
order-book read.

- Two emergency flattens ⇒ **one** market SELL. Reliably, across repeated runs.
- Two protective replacements ⇒ **two** stop orders. Reliably.

The only structural difference is that the emergency path writes a durable
intent with a generation counter and lets the unique index on
`idempotency_key` settle the tie, while the protective path writes nothing and
uses a random client id. The system is therefore already demonstrating that the
intent is what makes an operation safe under concurrency — which is the fix
proposed for P0-1, argued by its own behaviour rather than by assertion.

---

## P1 — production correctness

| # | Gap | Audit | Fix shape |
| --- | --- | --- | --- |
| P1-1 | `cancel_order` called directly from `reconcile.py:794`, outside the execution layer; the AST guard covers only `place_order` | §5.5 | Route the cancel through `ExecutionService`; extend the static guard to both primitives |
| P1-2 | Protective placement, resize and cancel have no durable intent; `IntentPurpose.PROTECTIVE_EXIT` is declared and unreferenced | §5.4 | Prerequisite of P0-1 |
| P1-3 | No orphan protective-order sweep — stray SELLs accumulate undetected | §5.1 | Part of P0-1 |
| P1-4 | No instrument-eligibility gate before capital moves; `LiquidityPolicy.allowed_symbols` defaults to `None`, so OTC is unblocked on the Alpaca path | §5.6 | An `InstrumentEligibilityGate` asserting secType, currency, venue, non-OTC for the broker in use |
| P1-5 | Bar freshness unchecked — the quote has a 15 s age limit, the daily bars behind ADV have none; `check_tradability_gate` holds the `STALE_DATA` check and has no caller | §3.1, §5.8 | A `DataFreshnessGate` covering every fact the decision consumes |
| P1-6 | Opportunity and exit claims are atomic within one process only — `threading.Lock` with no row lock or conditional update | §5.7, §6.2 | Conditional `UPDATE … WHERE status = :from`; document single-worker as an invariant until then |
| P1-7 | No test has ever issued the two POSTs that move capital; `tests/integration/` is an empty `__init__.py` | §4.1 | Phase 1 — this is the gap that hid the liquidity defect |
| P1-8 | Gate order is an emergent property of a 357-line `decide()` rather than a declared sequence | §3.1 | Phase 2 `DecisionPipeline` |
| P1-9 | `create_broker` and `create_market_data_port` are called per symbol inside the scan loop; neither caches | §7.1 | Per-cycle context. Blocks IBKR Paper certification: one TWS session per symbol is not viable |
| P1-10 | Published cards carry the risk verdict and size computed when that symbol was scanned, possibly a whole cycle earlier | §7.2 | Softened by approval re-deriving risk; the displayed size can still differ from the executed one |
| P1-11 | Without `REDIS_URL` the kill switch is a local file; two containers disagree about whether trading is halted | §6.5 | Require Redis in any multi-replica configuration, or fail startup |
| P1-12 | No database constraint prevents two open positions for one symbol; the invariant is Python-only | §6.1 | Partial unique index on `(symbol)` where `status = 'open'` |

---

## P2 — observability and operations

| # | Gap | Audit |
| --- | --- | --- |
| P2-1 | No metrics of any kind — none of the scanner, gate, execution, broker, safety or trading series in Phase 43 exists | — |
| P2-2 | No alerting: no path from `ProtectionUnverified`, `UNKNOWN`, orphan position or stale reconciliation to an operator who is not looking at the screen | — |
| P2-3 | Scan duration is not computed and overrun is not detected, though start and finish are timestamped | §7.6 |
| P2-4 | Funnel mis-accounting: no `capacity_rejected`; publish-time duplicates counted as published; symbols outside the slice in no bucket; early-exit cycles leave stale counters | §7.5 |
| P2-5 | `BOARD` and `DESK_BUS` are process memory, so agent status, funnel and SSE revisions diverge per worker | §6.2 |

---

## P3 — cleanup

| # | Gap | Audit |
| --- | --- | --- |
| P3-1 | Dead code: `check_tradability_gate` has no caller; the `entry * 0` quantity branch in `decide_exit` (`execution.py:1630`) is unreachable | §5.8 |
| P3-2 | Ranking has no explicit final tie-breaker; determinism rests on stable sort over traversal order and would break the moment scanning became concurrent | §7.3 |
| P3-3 | Ranking is scoped to the 60-name rotating slice, not the universe | §7.4 |
| P3-4 | `trading/execution.py` is ~1900 lines carrying entry, exit, emergency, protection, recovery and settlement | Phase 4 |

---

## Dependency graph

```
                      ┌──────────────────────────────┐
                      │ Phase 1 integration harness  │  (P1-7)
                      │ production wiring, fake      │
                      │ broker at the HTTP boundary  │
                      └───────────────┬──────────────┘
                                      │ every fix below needs
                                      │ red-green evidence here
                      ┌───────────────▼──────────────┐
                      │ P0-4 single-flight           │
                      │ reconciliation, off the GET  │
                      └───────┬──────────────┬───────┘
                              │              │
              ┌───────────────▼──┐      ┌────▼─────────────────┐
              │ P1-2 durable     │      │ P0-2 background      │
              │ protective intent│      │ reconciliation loop  │
              └───────┬──────────┘      └────┬─────────────────┘
                      │                      │
              ┌───────▼──────────┐      ┌────▼─────────────────┐
              │ P0-1 no duplicate│      │ P0-5 RECONCILIATION_ │
              │ stop + P1-3 sweep│      │ STALE gate           │
              └──────────────────┘      └──────────────────────┘

  P0-3 (exit atomicity)  — independent, needs only the harness
  P1-1 (cancel via service) — independent, pairs with P1-2
  P1-4 / P1-5 (eligibility, freshness) → feed Phase 2 DecisionPipeline
  P1-9 (per-cycle context)  — independent, gates IBKR certification
  Phase 4 decomposition — strictly last; needs all of the above green
```

Two orderings are forced and worth stating plainly, because getting them
backwards makes things worse rather than better:

**A background reconciliation loop must not land before reconciliation is
single-flight.** Today the race in P0-1 needs two overlapping HTTP requests.
Adding a timer without a guard adds a second, permanently running trigger and
makes the duplicate-stop race routine instead of occasional.

**The stale-reconciliation gate must not land before the loop.** Reconciliation
currently runs only when someone is watching the dashboard, so a gate on its age
would refuse essentially every entry, and the natural response to that would be
to widen the threshold until the gate means nothing.

---

## Proposed implementation order

1. **Phase 1 harness** — `tests/integration/`, production wiring, fake broker at
   the vendor HTTP boundary (`FakeAlpacaBackend` already drives the real
   adapter). Tests 1–9 first: every mandatory gate refusing, asserted as *zero
   broker mutations*, entered through `POST /api/v1/opportunities/{id}/decide`.
2. **Tests 10–24** — the happy path, idempotency, partial fills, restart
   recovery, reconciliation and concurrency. Several are expected to fail on
   arrival; those failures are the red half of the red-green evidence for the
   P0 fixes.
3. **P0-4** then **P1-2 → P0-1 + P1-3** then **P0-2** then **P0-5**, in that
   order, for the reason above.
4. **P0-3** — exit atomicity. Independent; can run in parallel with 3.
5. **P1-1** — cancel through the service; widen the static guard to
   `cancel_order`.
6. **Phase 2 `DecisionPipeline`** — absorbing P1-4 and P1-5 as declared gates
   rather than new conditionals.
7. **P1-9** — per-cycle scan context. Required before any IBKR Paper work.
8. **P2** — metrics, then alerts.
9. **Phase 4** — execution decomposition, behaviour-preserving, one extraction
   at a time with the integration suite green after each.

Nothing in P3 starts while anything in P0 or P1 is open.

---

## Files expected to change

| Area | Files |
| --- | --- |
| Harness | `tests/integration/conftest.py` (new), `tests/integration/test_*.py` (new) |
| P0-4, P0-2 | `api/routes/desk.py`, `api/main.py`, `trading/reconcile.py`, a new `trading/reconcile_loop.py` |
| P0-1, P1-2, P1-3 | `trading/execution.py`, `trading/order_intent.py`, `trading/reconcile.py`, `core/enums.py` (already has the member) |
| P0-3 | `trading/intents.py`, `trading/ledger.py` |
| P0-5 | `trading/gates.py`, `trading/execution.py`, `core/config.py` |
| P1-1 | `trading/reconcile.py`, `trading/execution.py`, `tests/unit/test_capital_safety.py` |
| P1-4, P1-5 | `trading/gates.py`, `quant/filters.py`, `trading/execution.py` |
| P1-9 | `trading/pipeline.py`, `agents/scanner/agent.py`, `agents/supervisor/agent.py` |
| P1-12 | `database/models/positions.py`, new Alembic revision |
| Docs | `ARCHITECTURE.md`, `AGENTS.md`, `docs/architecture/execution-failure-matrix.md`, `docs/architecture/staged-plan.md` |

## Migration risks

Only two changes touch the schema. Note that P0-6 needs none: it is a change of
which number the comparison reads.

**P1-12, partial unique index on open positions.** SQLite and PostgreSQL both
support partial indexes, with different syntax; the revision must branch on
dialect. The migration will fail on any database that already contains two open
rows for one symbol, which is the point — but it needs a pre-flight query and a
documented manual resolution, not a stack trace at deploy time.

**P1-2, protective intents.** Additive only: new `purpose` values in an existing
column. `IntentPurpose.PROTECTIVE_EXIT` already exists in `core/enums.py`, and
migration `0005` already added the `purpose` column. No new column is required,
so downgrade is a no-op.

## Backward-compatibility risks

- **Existing unprotected positions.** Once protective placement requires a
  durable intent, positions already open with a stop recorded only in
  `payload.stop_order_id` have no intent. Reconciliation must adopt them —
  create the intent from observed broker state — rather than treating them as
  unprotected and placing a second stop, which would reproduce P0-1 during the
  very deploy that fixes it.
- **`RECONCILIATION_STALE` on a cold start.** Immediately after a restart there
  is no successful reconciliation, so the gate refuses every entry until the
  first pass completes. Correct, but it must be visible on the dashboard as a
  named state rather than as an unexplained absence of proposals.
- **The desk payload contract.** Moving reconciliation off the GET changes when
  `last_success_at` advances. The frontend reads it (`ReconciliationBanner`);
  the field must keep its meaning or change in the same commit.

## Tests required before each refactor

Per the red-without-fix rule, each fix needs a test that fails against the
current code before it is written.

| Fix | Test that must be red first |
| --- | --- |
| P0-1 | Two concurrent reconciliation passes against one unprotected position ⇒ exactly one stop at the fake broker |
| P0-2 | With no HTTP traffic at all, an unprotected position is detected and protected within the configured interval |
| P0-3 | Crash injected between the ledger commit and the `applied_exit_qty` commit ⇒ replay reduces the position exactly once |
| P0-4 | Two overlapping `GET /desk/broker?fresh=true` ⇒ one reconciliation pass, one set of mutations |
| P0-5 | Approve with the last reconciliation older than the threshold ⇒ `RECONCILIATION_STALE`, zero mutations |
| P0-6 | Venue holds 25 against a book of 50 ⇒ no resting protective stop covers more than 25 |
| P1-1 | Static guard extended to `cancel_order` ⇒ fails on today's `reconcile.py:794` |
| P1-4 | Approve an OTC-shaped symbol on the Alpaca path ⇒ refused |
| P1-5 | Approve with bars a week old ⇒ refused |
| P1-9 | One scan cycle over N symbols ⇒ one broker construction, not N |
