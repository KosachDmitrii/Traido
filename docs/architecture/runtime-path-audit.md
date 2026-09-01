# Runtime path audit

Phase 0 of the production-hardening programme. This document exists because of
a specific incident: the liquidity gate was written, made fail-closed, covered
by unit tests and described in `ARCHITECTURE.md` as enforced — and the two HTTP
routes that authorize a trade both constructed `ExecutionService` without the
market-data dependency, so the gate returned "no failure" and never stood on the
path to a broker order. Every claim in this file is therefore about the *path*,
not the component.

**Rule this document enforces.** A safety component is `PROVEN` only when a test
starting from the real entry point — the route, the loop, the callback — reaches
the broker boundary and shows the mutation did not happen. A unit test that
constructs the service by hand proves the component, not the path.

Audited at: repository state of 2026-08-31. Every line reference is from that
state; re-derive them before trusting one that no longer matches.

> **This document is a snapshot of the defect, not of the code.** Phases 1 and 2
> have since closed every P0 and every P1 that gated IBKR Paper; the findings
> below are kept in their original wording so each fix can be read against the
> problem it answers, and so a future regression is recognisable as a return to
> something already understood. `gap-register.md` carries the current status and
> the test that proves each closure. Line numbers here are from the pre-fix
> state and will not match.
>
> Three findings changed shape rather than simply closing, and are worth naming
> here because the audit's own reasoning about them was incomplete:
>
> - **§5.2, reconciliation is not a control loop.** Correct, and the fix was a
>   background loop — but the loop had to land *after* single-flight, because a
>   timer without a guard turns an occasional race into a scheduled one.
> - **§5.1, duplicate protective stops.** The audit read this as a missing
>   idempotency key. It is that, but the harness showed a second half: the
>   recovery lookup consulted a cached open-order book, so a stop that *was*
>   resting could be missed and replaced. A durable key without a truthful read
>   would have moved the duplicate rather than removed it.
> - **§6.1, one open position per symbol.** Enforced in Python twice and in the
>   database not at all. The index closes it — and the ledger now translates the
>   database's refusal into the same `DuplicateOpenPosition` the callers already
>   handle, so the invariant reads the same from either side.

---

## 1. Broker mutation primitives

`BrokerPort` (`core/ports.py:61`) exposes exactly two methods that change state
at the broker:

| Primitive | Signature | Call sites outside `broker/` |
| --- | --- | --- |
| `place_order` | `core/ports.py:75` | 5, all in `trading/execution.py` |
| `cancel_order` | `core/ports.py:77` | 3 — two in `trading/execution.py`, **one in `trading/reconcile.py`** |

Everything else on the port is a read. The audit below traces every route, loop
and callback that can reach either primitive.

`tests/unit/test_capital_safety.py:221` already guards `place_order` against
call sites outside `broker/`, `trading/execution.py` and `core/ports.py`. It
does **not** guard `cancel_order`, which is how `trading/reconcile.py:794`
came to call the broker directly.

---

## 2. Path inventory

Eleven distinct paths can reach a broker mutation. Three HTTP entry points; no
background job mutates anything today, which is itself a finding (§5.2).

| # | Effect | Entry point | Chain | Durable intent |
| --- | --- | --- | --- | --- |
| P-1 | ENTRY | `POST /api/v1/opportunities/{id}/decide` | `api/routes/trading.py:48` → `build_execution_service` → `decide()` `execution.py:213` → `_place_entry` `:739` → `place_order` `:773` | Yes — `entry:{opp}:{n}` |
| P-2 | PROTECTIVE ORDER | same route | `decide()` → inline `place_order` `execution.py:462` | **No** |
| P-3 | EMERGENCY_EXIT | same route | `decide()` → `_emergency_flatten` `:1221` → `place_order` `:1262` | Yes — `emergency_exit:{pos}:{reason}:{gen}` |
| P-4 | ORDER CANCEL | same route | `decide()` → `_settle_stalled_entry` `:888` → `cancel_order` `:922` | Covered by the entry intent |
| P-5 | EXIT | `POST /api/v1/exits/{id}/decide` | `api/routes/desk.py:423` → `decide_exit()` `execution.py:1537` → `_place_exit` `:1739` → `place_order` `:1788` | Yes — `exit:{pos}:{gen}` |
| P-6 | ORDER CANCEL | same route | `_place_exit` → `_cancel_quietly` `:1212` → `cancel_order` `:1215` | **No** |
| P-7 | PROTECTIVE ORDER | `GET /api/v1/desk/broker` | `_build_broker_snapshot` `desk.py:219` → `reconcile_positions` `reconcile.py:106` → `reconcile_protective_orders` `:584` → `ensure_protection` `execution.py:1060` → `place_order` `:1048` | **No** |
| P-8 | PROTECTIVE RESIZE | `GET /api/v1/desk/broker` | … → `_resize_stale_protection` `reconcile.py:735` → `resize_protection` `execution.py:1108` → cancel then place | **No** |
| P-9 | EMERGENCY_EXIT | `GET /api/v1/desk/broker` | … → `ensure_protection` returns `None` → `_emergency_flatten` | Yes |
| P-10 | ORDER CANCEL | `GET /api/v1/desk/broker` | … → `cancel_orphaned_entry_orders` `reconcile.py:766` → `broker.cancel_order` `:794` — **does not pass through `ExecutionService`** | **No** |
| P-11 | (no mutation) | scanner loop `agents/scanner/agent.py` | publishes opportunities only; it is the *source* of P-1 but touches no broker mutation | n/a |

### 2.1 A GET request mutates the broker

P-7 through P-10 are reachable by `GET /api/v1/desk/broker`. That request can
place a stop order, cancel and re-place a stop order, emergency-close a
position, and cancel resting entry orders. `?fresh=true` (`desk.py:352`) bypasses
both the 15-second response cache and the 30-second reconciliation interval.

The consequences are operational, not merely stylistic. A browser prefetch, a
retried request, an uptime probe pointed at the wrong URL, or two dashboard tabs
open side by side all trigger broker mutations, and nothing serialises them.

### 2.2 The comment at `desk.py:239` is wrong

> Reconciliation finds unprotected positions but never places orders itself; the
> execution service remains the only path.

True for placement, false for cancellation: P-10 calls `broker.cancel_order`
directly from `reconcile.py:794`.

---

## 3. Gate chains, in execution order

### 3.1 Entry (P-1)

Order as actually executed by `decide()`:

| # | Check | Line | Fails closed? |
| --- | --- | --- | --- |
| 1 | status / expiry | `:221`–`:242` | yes |
| 2 | kill switch | `:266` | yes |
| 3 | atomic claim `AWAITING → APPROVING` | `:269` | yes |
| 4 | `RiskEngine.evaluate` — includes event risk, exposure, correlation, sizing | `:284` | yes |
| 5 | RTH | `:594` via `_entry_gates` | yes |
| 6 | broker connectivity `READY` | `:598` | yes |
| 7 | market-data port present | `:602` | yes (since 2026-08-30) |
| 8 | bars retrievable | `:622` | yes |
| 9 | liquidity: price, ADV, current volume, spread, participation, slippage | `:642` | yes |
| 10 | unresolved (`UNKNOWN`) intent for symbol | `:318` | yes |
| 11 | one open position per symbol | `:334` | yes |

Absent from this chain:

- **Reconciliation freshness.** Nothing consults how long ago reconciliation
  last succeeded. `_LAST_RECONCILE_WALL` exists (`desk.py:52`) and is rendered
  in the desk payload (`desk.py:337`), but no code reads it to refuse anything.
- **Instrument eligibility.** No gate asserts `secType`, currency, listing
  venue or non-OTC before risk. `LiquidityPolicy.allowed_symbols` defaults to
  `None` (`trading/gates.py:132`), so the OTC block is unarmed by default. IBKR's
  resolver enforces this, but only on the IBKR adapter, and only at submit time.
- **Bar freshness.** The quote carries an age check (15s, `gates.py:142`). The
  daily bars behind ADV and current-volume do not: a provider returning
  week-old bars satisfies the gate. `check_tradability` has a `STALE_DATA`
  check (`quant/filters.py:53`) and **no production caller**.

Ordering note: risk — the most expensive check, which fetches the portfolio and
may make a vendor call for the earnings calendar — runs before RTH and before
connectivity. Not a safety defect, but the ordering is an emergent property of
a 357-line method rather than a declared sequence.

### 3.2 Exit (P-5)

| # | Check | Line |
| --- | --- | --- |
| 1 | status | `:1549`–`:1557` |
| 2 | kill switch | `:1578` |
| 3 | atomic claim | `:1581` |
| 4 | `_exit_gate`: broker `READY`, no emergency flatten in flight | `:1594` |

Correctly *not* gated on RTH or liquidity: a protective exit must not be blocked
because new entries are blocked. Also missing reconciliation freshness, which
matters less here for the same reason.

### 3.3 Reconciliation-driven mutations (P-7 … P-10)

No gates at all beyond what `ExecutionService` applies internally. In
particular there is no kill-switch check on the reconciliation path. That is
arguably correct for risk-reducing actions (Phase 45 requires
`PAUSE_NEW_ENTRIES` to allow protective risk reduction) but it is undocumented
and untested, so it is currently accidental rather than designed.

---

## 4. Enforcement status

Status vocabulary: `PROVEN` = an end-to-end test from the real entry point
demonstrates it; `PARTIALLY_PROVEN` = a unit test proves the component but not
that the path reaches it; `UNPROVEN` = no test; `BROKEN` = the invariant does
not hold.

| Path | Invariant | Enforcement in code | Test proving it | Status |
| --- | --- | --- | --- | --- |
| P-1 | Kill switch blocks entry | `execution.py:266` | `test_stage4_trading.py::test_kill_switch_blocks_approve` (hand-built service) | PARTIALLY_PROVEN |
| P-1 | Risk FAIL ⇒ 0 mutations | `:290` | `test_earnings_fail_closed.py`, `test_risk_*` (hand-built) | PARTIALLY_PROVEN |
| P-1 | RTH blocks entry | `:594` | `test_entry_gate_enforcement.py` (hand-built) | PARTIALLY_PROVEN |
| P-1 | Broker not READY blocks entry | `:598` | `test_entry_gate_enforcement.py` (hand-built) | PARTIALLY_PROVEN |
| P-1 | No market-data port ⇒ refuse | `:602` | `test_liquidity_gate_is_armed.py` — includes an AST test that no route builds its own service | PARTIALLY_PROVEN (strongest of the set) |
| P-1 | Missing/stale quote ⇒ refuse | `gates.py:190` | `test_gates.py`, `test_entry_gate_enforcement.py` (hand-built) | PARTIALLY_PROVEN |
| P-1 | `UNKNOWN` blocks a conflicting entry | `:318` | `test_execution_idempotency.py` (hand-built) | PARTIALLY_PROVEN |
| P-1 | One open position per symbol | `:334` + `trading/ledger.py` | `test_execution_idempotency.py`, ledger tests | PARTIALLY_PROVEN |
| P-1 | Stale reconciliation blocks entry | **absent** | none | BROKEN |
| P-1 | Instrument eligibility (non-OTC, STK, USD) | absent on this path | none | BROKEN |
| P-1 | Bar data freshness | absent | none | BROKEN |
| P-1 | Duplicate approval ⇒ one order | `store.claim` + `create_or_get` | `test_stage4_trading.py::test_approve_is_idempotent` (sequential, not concurrent) | PARTIALLY_PROVEN |
| P-1 | Two *concurrent* approvals ⇒ one order | `threading.Lock` only — process-local (§5.7) | none | BROKEN across processes |
| P-2 | Protective stop is durable and single | **no intent, no key** | none | BROKEN |
| P-3 | Emergency exit durable, one per generation | `:1344` | `test_lifecycle.py` (hand-built) | PARTIALLY_PROVEN |
| P-5 | Exit durable and idempotent | `exit:{pos}:{gen}` | `test_exit_reconciliation.py` (hand-built) | PARTIALLY_PROVEN |
| P-5 | Partial exit reduces by filled qty only | `apply_exit_to_ledger` | `test_exit_reconciliation.py` | PARTIALLY_PROVEN |
| P-7 | Protection is verified, not assumed | `reconcile.py:584` | `test_exit_reconciliation.py`, `test_reconciliation_*` | PARTIALLY_PROVEN |
| P-7 | Unreadable broker ⇒ `ProtectionUnverified` | `reconcile.py:619` | unit test exists | PARTIALLY_PROVEN |
| P-7 | Never place a second protective order for the same shares | **absent** | none | BROKEN |
| P-7/P-8 | Two reconciliation passes are idempotent | **absent** — no lock, no intent | none | BROKEN |
| P-10 | Cancels go through the execution service | **violated** | AST test guards `place_order` only | BROKEN |
| all | Reconciliation runs continuously | **no scheduler** — driven by UI polling | none | BROKEN |
| all | Live trading refused | `core/config.py:49`, called from `get_settings()` | `test_capital_safety.py:185` | PROVEN |
| all | LLM cannot reach the broker or SQL | no `create_llm` caller exists | `test_capital_safety.py:204` | PROVEN (vacuously — no LLM runs) |

### 4.1 Route-level coverage is zero

Only two test modules use `TestClient` (`test_evaluation_api.py`,
`test_desk_split.py`). The POST routes they exercise: `/api/v1/scanner/run`.

**No test has ever issued `POST /api/v1/opportunities/{id}/decide` or
`POST /api/v1/exits/{id}/decide`.** The two endpoints that move capital have no
route-level coverage of any kind. Every "PARTIALLY_PROVEN" above is partial for
exactly this reason, and it is the same gap that let the liquidity wiring defect
survive from Stage 4 to Stage 7.1.

`tests/integration/` contains a single empty `__init__.py`.

---

## 5. Findings

### 5.1 Duplicate protective order (capital safety)

`reconcile_protective_orders` (`reconcile.py:584`) reads the open-order book,
finds the recorded `stop_order_id` absent, and places a replacement. There is no
durable intent, no idempotency key and no lock. Two concurrent passes — which
`GET /api/v1/desk/broker?fresh=true` makes trivially reachable — both observe
"unprotected" and both place a stop for the full position size. The second
`ledger.set_stop_order_id` (`:674`) overwrites the first, so the book retains one
id and the other stop becomes an orphan resting SELL.

When the position is later exited, that orphan can execute against shares that
no longer exist, opening a short position in a system whose policy disables
shorting.

Compounding it: the loop only asks "does the stop I recorded still exist". It
never asks "are there SELL orders at this broker I do not know about", so an
orphan is never detected afterwards. `cancel_orphaned_entry_orders` sweeps stray
BUYs; there is no equivalent for stray SELLs.

### 5.2 Reconciliation is not a control loop

`api/main.py:81` starts exactly one background task, `start_scanner()`.
Reconciliation runs only inside `_build_broker_snapshot`, called by
`GET /api/v1/desk/broker`.

If nobody has the dashboard open, then for as long as that is true: protective
orders are not verified, orphan positions are not detected, `UNKNOWN` intents
are never resolved, and broker discrepancies are never found. The system's
mechanism for learning what the broker actually holds depends on a human having
a browser tab open. Overnight, that is the normal state.

### 5.3 Nothing refuses to trade on stale broker truth

Reconciliation age is computed and displayed but never enforced (`desk.py:339`).
An entry can be approved while the last successful reconciliation is arbitrarily
old — including "never", since `_LAST_RECONCILE_WALL` starts as `None`.

### 5.4 Protective orders have no durable identity

`IntentPurpose.PROTECTIVE_EXIT` is declared (`core/enums.py:82`) and referenced
in one documentation line. No code constructs an intent with it. Protective
placement, resize and cancel are broker mutations with no durable record before
submission, which is the one rule the entry and exit paths do observe.

### 5.5 `cancel_order` escapes the execution layer

`reconcile.py:794`. The architectural test that would have caught it checks only
`place_order`.

### 5.6 Instrument eligibility is not gated before capital moves

The V1 policy — US-listed stocks and ETFs, non-OTC, price ≥ 10 USD — is enforced
by the IBKR resolver at submit time and by `LiquidityPolicy.min_price` in the
gate. `allowed_symbols` is `None` by default, so nothing blocks OTC on the
Alpaca path. There is no gate that asserts security type, currency or listing
venue for the broker actually in use.

### 5.7 The "atomic" opportunity claim is atomic within one process only

`OpportunityStore.claim` (`trading/opportunities.py:96`) is documented as an
"atomic status transition". It reads the row, compares the status in Python, and
writes the new one, serialised by `self._lock` — a `threading.Lock` held by the
process that happens to own the object. There is no `SELECT … FOR UPDATE`, no
conditional `UPDATE … WHERE status = :from`, and no version column.

With one uvicorn worker this is correct. With two workers or two containers,
both can read `AWAITING_CONFIRMATION`, both write `APPROVING`, and both proceed
into `decide()`.

Two later layers do catch it, which is why this is a correctness gap rather than
an immediate capital hazard:

1. `order_intents.idempotency_key` carries a real unique index
   (`database/models/desk.py:56`, migration `0004:41`). Both processes compute
   `entry:{opp.id}:0`, one insert wins, and `create_or_get` hands the loser the
   same intent row.
2. `client_order_id` is derived from that shared intent id
   (`execution.py:744`), so if both still reach `place_order` before either
   transitions to `SUBMITTING`, they submit the *same* client id and the broker
   rejects the second.

So the real dependency is on broker-side client-id deduplication. That is a
reasonable last line of defence, but it is undocumented, untested, and differs
between Alpaca and IBKR. Single-worker deployment is likewise a deployment
accident: nothing in the code, configuration or documentation states it.

### 5.8 Dead safety code

`check_tradability_gate` (`trading/gates.py:238`) has no caller anywhere.
It contains the `STALE_DATA`, `VOLATILITY_TOO_LOW` and `PRICE_TOO_LOW` checks
that the live path does not perform.

---

## 6. Durability and concurrency of the state the paths depend on

### 6.1 The schema has one constraint

Across `database/models/` and all five Alembic revisions there is exactly one
uniqueness guarantee: `order_intents.idempotency_key`
(`database/models/desk.py:56`, migration `0004:41`). There are **no foreign
keys and no CHECK constraints anywhere in the schema**.

Consequences that matter for the paths above:

- **`client_order_id` is not a column.** It lives inside the intent's JSON
  payload (`trading/order_intent.py:287`). It cannot be indexed or made unique,
  and recovery-by-client-id must scan payloads. The broker-side dedup that §5.7
  leans on has no local counterpart.
- **Nothing in the database prevents two open positions for one symbol.**
  `open_positions` indexes `symbol` and `status` separately and non-uniquely
  (`database/models/positions.py:23`). The invariant is Python-only, inside a
  `threading.Lock`, as a SELECT-then-INSERT (`trading/ledger.py:111`).
- `position_id`, `opportunity_id` and `backtest_run_id` are unconstrained
  UUID columns. An orphaned reference is possible and undetectable.

### 6.2 Every store lock is process-local

`OpportunityStore`, `ExitStore`, `OrderIntentStore` and `PositionLedger` are all
database-backed — data survives restart — but each serialises its
read-modify-write sequences with a `threading.Lock` owned by the process
(`opportunities.py:57`, `exits.py:65`, `intents.py:108`, `ledger.py:73`). No
code anywhere uses `SELECT … FOR UPDATE`, a conditional
`UPDATE … WHERE status = :from`, or a version column.

`BOARD` (`core/activity.py:137`) and `DESK_BUS` (`core/desk_bus.py:97`) are
pure process memory: with two workers, agent status, funnel counters, SSE
revisions and ETags diverge per worker and the dashboard shows whichever worker
answered.

`OrderIntentStore.transition` (`intents.py:140`) is last-writer-wins. Two
workers can both read `SUBMITTING` and both write a different terminal state
with no conflict detected.

### 6.3 `apply_exit_to_ledger` is two transactions

```
delta = filled_qty - intent.applied_exit_qty
LEDGER.apply_exit_fill(...)             # commits
store.update_fields(applied_exit_qty=…)  # commits separately
```

`trading/intents.py:273`–`309`. A crash — or a second reconciliation pass —
between the two commits reduces the position twice for one fill, or loses the
`applied_exit_qty` bookkeeping that makes the operation idempotent. This is the
exact mechanism the partial-exit design relies on, and it is not atomic.

`PositionLedger.set_stop_order_id` (`ledger.py:148`) runs in its own
transaction with **no lock at all** — it is the write that records which stop
belongs to a position, and it is the write that loses one of two racing stops in
§5.1.

### 6.4 Protective recovery is deliberately non-idempotent at the broker

`_place_protective_stop` builds its client id as
`traido-s-recover-{uuid4()}` (`execution.py:1038`). The entry path derives its
client id from the durable intent id, so the broker can reject a duplicate; the
protective path generates a fresh random id every call, so the broker
*cannot*. Two racing protective placements are guaranteed to produce two
distinct orders.

The resize path (`execution.py:1140`) cancels the recorded stop and then places
a new one. Two concurrent resizers cancel each other's replacements.

### 6.5 Kill switch without Redis needs a shared filesystem

`risk/kill_switch.py:32` writes `data/kill_switch.on`; Redis
(`REDIS_KEY`, `:33`) is used only when `REDIS_URL` is set and writes to it are
best-effort (`:160`–`:176`). With no Redis and more than one container, each
container reads its own local file and they disagree about whether trading is
halted. The state reports `source="degraded"` in that case (`:141`) but
`enabled` is still answered from the file.

---

## 7. Scanner

The scanner reaches no broker mutation, but it decides what P-1 is ever offered,
so its biases are capital-relevant.

### 7.1 A fresh broker and data client per symbol

`trading/pipeline.py:80` calls `create_broker(settings)` inside
`run_symbol_pipeline`, and `:97` calls `create_market_data_port(settings)`.
Neither factory caches (`broker/factory.py:18`, `market_data/factory.py:9`), so
a 60-symbol cycle constructs 60 broker adapters and 60 data clients, each
fetching the portfolio and the position list again.

For Alpaca this is connection churn. For IBKR it is a blocker: `IBKRBroker()`
per symbol means a TWS session per symbol, and IB rejects concurrent
`clientId` reuse. Phase 41 cannot pass while this stands.

### 7.2 There is no per-cycle context

Portfolio, cash and positions are fetched per symbol
(`pipeline.py:88`, `risk/context_builder.py:59`), so every symbol is judged
against a slightly different account state and the market regime is re-fetched
per symbol (`agents/supervisor/agent.py:110`).

At publish time (`pipeline.py:157`) the only thing re-checked is whether an open
opportunity already exists for the symbol. The risk verdict and `sized_qty`
carried into the desk card are the ones computed when that symbol was scanned,
which may be the whole cycle ago. The cycle has no timeout, and a 60-symbol pass
makes many HTTP calls per symbol with 8–30 s timeouts each, so the upper bound
on that staleness is not bounded by anything in the code.

This is materially softened by `decide()` re-deriving the risk context at
approval (`execution.py:283`) — the stale number is displayed, not acted on.
The displayed size can still differ from the size that would execute.

### 7.3 Ranking has no final tie-breaker

`rank_key` returns `(confidence, risk_reward)` (`agents/scanner/agent.py:353`).
When both are equal the outcome falls to Python's stable sort over the pre-sort
order, which is universe traversal order (`:288`). Deterministic today, because
the universe list is fixed and the loop is sequential — but it is determinism by
accident: it would break the moment scanning became concurrent, which Phase 19
asks for.

### 7.4 Ranking is over the slice, not the universe

`max_symbols_per_cycle` is 60 (`configs/watchlist.json:19`) and the cycle ranks
only the names in that rotating window (`:254`, `:383`). A symbol outside the
window cannot win a slot however strong it is. Rotation
(`_scan_cursor`, `:326`) spreads the window over time but never produces a
per-cycle global optimum, and symbols outside the window receive no funnel
accounting at all — `considered` is the slice size, not the universe size.

### 7.5 Funnel accounting gaps

Real counters: `considered, scanned, errored, no_candidate, candidates,
risk_rejected, passed, opportunities, outranked, already_open`
(`agents/scanner/agent.py:44`). Against the required funnel:

- No `capacity_rejected` — candidates displaced by a full desk are folded into
  `outranked`, so "lost to a better name" and "no slot existed" are
  indistinguishable.
- Publish-time duplicates are **mis-counted**: `publish_opportunity` returns the
  pre-existing opportunity (`pipeline.py:159`) and `_offer_best` still
  increments `offered` (`:388`), so `opportunities` over-reports new cards.
- Symbols outside the slice are in no bucket.
- Cycles that exit early on kill switch or disabled state (`:255`) return before
  `funnel.reset()`, leaving the previous cycle's numbers on the board.

### 7.6 Scheduling

`scanner_loop` (`:428`) is sleep-after-finish, not fixed cadence: cycle
duration adds to the interval. `last_started_at` and `last_finished_at` are
recorded (`:264`, `:323`) but no duration is computed and no overrun is
detected or logged. Overlap is prevented by `_cycle_active` (`:211`), a plain
module boolean that a second process does not share.

There is no per-symbol timeout around `run_symbol_pipeline` (`:300`). The
vendor HTTP clients do set timeouts (8–30 s), so a hang requires a dependency
that bypasses them, but nothing bounds the cycle itself.

---

## 8. What Phase 1 proved

`tests/integration/` now drives the desk over HTTP against a fake venue, with
only the vendor boundaries replaced. Broker mutations are counted at the
transport, so "zero broker mutations" means zero requests left the process.

**Red-without-fix evidence.** The historical defect was reintroduced —
`build_execution_service` stopped passing `market_data`, and `_entry_gates`
returned `None` when it was absent, exactly as shipped. Six tests turned red
(tests 1, 2, 3, 4, 4b, 4c) while the RTH, connectivity, earnings, unresolved-state
and kill-switch tests stayed green, showing the failures are specific to the
wiring rather than incidental. The code was then restored and the suite returned
to green.

**Confirmed working from the route inwards**, which the audit could not
establish by reading: every entry gate refuses with the right reason and no
order leaves; a partial entry becomes a position of the filled size with
protection sized to match; the unfilled remainder is cancelled without discarding
the fill; a failed protective stop triggers a durable emergency flatten; an
unreadable order book produces `ProtectionUnverified` and no orders; a lost
submit reply leaves `UNKNOWN` with the client id persisted, blocks the symbol,
and does not resubmit across a restart; two simultaneous approvals in one
process place one order; a venue-side shrink that no exit explains blocks the
symbol instead of being absorbed.

**Confirmed broken.** §5.1 (duplicate protective stop) reproduces
deterministically once the interleaving is pinned. §5.3 (no stale-reconciliation
gate) is red by construction. And a defect not found by reading at all: with the
book and the venue in known disagreement, the protective stop is validated
against the book, leaving a resting SELL for twice what is held — recorded as
P0-6 in the gap register.

Statuses in §4 should be re-read against this: the rows for entry gates,
partial fills, protection sizing and emergency durability are now `PROVEN`
rather than `PARTIALLY_PROVEN`. The rows marked `BROKEN` remain broken, and now
have failing tests attached rather than arguments.

## 9. What Phase 1 does not yet prove

The gaps above are hypotheses about the runtime path until a test starting at
the route demonstrates them. Phase 1 builds that harness. Until then, every row
in §4 marked `BROKEN` is asserted from code reading, and every row marked
`PARTIALLY_PROVEN` should be read as "the component works when called; nothing
proves the route calls it".
