# Staged Implementation Plan

## Stage 0 — Architecture freeze — done
## Stage 1 — Market data + Quant — done
## Stage 2 — Backtest + journal — done
## Stage 3 — Agents V1 — done

## Stage 4 — Risk + Confirm + Alpaca Paper
- [x] Deterministic RiskEngine + sizing
- [x] Kill switch (file-backed)
- [x] Opportunity store + Confirm API (approve/skip)
- [x] ExecutionService (only code path to BrokerPort)
- [x] Alpaca Paper adapter + MockPaperBroker for tests
- [x] Preview BUY / SKIP wired to API
- [x] Hardening: SQL opportunities/exits/audit, API key / local-only auth, SELL via ExecutionService, idempotent approve
- [x] Scan coverage: `max_open_buy_opportunities` is a queue cap, not a risk
      control — it bounds how many proposals may await a human. The cycle
      reports a pause distinctly from "scanned everything, found nothing", the
      universe is walked round-robin so the cap cannot hide its tail
      permanently, and a cleared queue wakes the scanner instead of waiting out
      the interval it earned by scanning nothing.
- [x] Proposal selection: a pass evaluates the whole window before offering
      anything, then gives the free slots to the strongest by `confidence`
      (`risk_reward` breaks ties). About one symbol in five clears risk, so
      publishing as you go filled the desk a third of the way through the
      universe and showed the earliest qualifiers rather than the best. A
      proposal already on the desk is never displaced: withdrawing a card races
      with the click it is waiting for. Ranking is deliberately *not* a scoring
      model of its own — see Stage 8

## Stage 5 — Position + Review
- [x] Position Agent exit proposals (structure / RR / RSI / stale)
- [x] Open positions ledger + journal on close
- [x] Review Agent analytics (`/api/v1/review`)
- [x] Desk shows live portfolio / positions / win rate

## Stage 6 — Dashboard
- [x] Vite + React desk (`frontend/`) on locked design tokens
- [x] Separated from Python backend (`backend/README.md` · packages under `backend/`)
- [x] Live desk: stats, positions, review, agents, BUY/SELL rail
- [x] Proxy `/api/*` → FastAPI

## Desk lifecycle upgrade (pre–Stage 7)
- [x] Fill-aware entry (wait fill → ledger at fill price)
- [x] Protective stop hard-fail → emergency flatten + discard
- [x] Exit journal at sell fill price; cancel resting stop
- [x] Reconcile ledger↔broker on desk poll
- [x] Strategy confluence `strategy_confluence@0.2.0` (D1 trend + pullback)
- [x] Risk book exposure + drawdown telemetry

---

Stages below derive from `ARCHITECTURE.md` (v1.0, conceptually frozen) and are
ordered by capital risk, not by convenience. Nothing here starts without
explicit approval.

## Stage 7 — Execution integrity
The gap between "the desk works" and "the desk is safe when things fail."

- [x] **Partial fill on timeout leaves an unprotected position** — cancel the
      remainder, then protect or flatten whatever actually filled. Invariant 7.
- [x] Durable order intent written *before* transmission; `idempotency_key`
      alongside `client_order_id` (`trading/order_intent.py`, `trading/intents.py`,
      `order_intents` table, migration `0004`)
- [x] Full order state machine incl. `UNKNOWN` / `EXPIRED` — `IntentStatus` with
      an explicit transition table; illegal moves raise
- [x] Recovery resolves an in-flight intent instead of re-sending a buy; broker
      lookup by our own `client_order_id` covers the lost-reply case
- [x] `UNKNOWN` blocks conflicting entries at both the execution service and the
      risk engine, and lifts only through reconciliation
- [x] Liquidity gate in the live path (`trading/gates.py` — price floor, average
      and current dollar volume, participation, estimated slippage, optional
      spread) enforced immediately before the broker call
- [x] RTH gate for new entries — computed NYSE calendar with holidays and early
      closes; protective exits and reconciliation are deliberately exempt
- [x] Reconciliation resolves intents, detects orphan positions, and reinstalls
      missing protective stops through `ExecutionService.ensure_protection`
- [x] Broker health monitor → anything other than `READY` disables new entries
      and discretionary exits (`BrokerConnectionState`, `check_connectivity`)
- [x] Fail-safe mode: no new positions, existing protective orders left in place
      rather than cancelled, reconciliation continues re-verifying them

## Stage 7.1 — Durable exit lifecycle + IBKR paper readiness
Everything Stage 7 deliberately deferred, plus what real IB connectivity needs.

- [x] One generalized durable intent covering `ENTRY`, `EXIT`, `EMERGENCY_EXIT`
      and `PROTECTIVE_EXIT` (`IntentPurpose`, migration `0005`) rather than a
      second parallel persistence system
- [x] Exit idempotency keyed on the position, not the exit card — two proposals
      for one position collapse onto one broker order
- [x] Exits reuse the entry state machine; recovery adopts a live order instead
      of sending a second sell
- [x] Emergency close is durable and cannot fire twice, including after it has
      already completed
- [x] Partial exit reduces the local position by the actual fill only; the
      ledger cannot close a position twice; staged exits journal a blended price
- [x] Protective stop is resized to the remaining quantity after a partial exit,
      restored after a failed exit, and repaired when the broker's resting stop
      does not match the position
- [x] Reconciliation covers exit intents and position quantities, and is
      idempotent across repeated passes (`applied_exit_qty`)
- [x] Spread checks fail closed without a fresh live quote; modeled spreads are
      labelled and cannot satisfy a live gate (`QuotePort`, `SpreadSource`)
- [x] Event risk fails closed on the same principle. Without a Finnhub key the
      calendar returned two empty dates, which the engine could not tell from
      "read it, nothing scheduled" — so every proposal cleared an earnings gate
      that had never run, and the log said so once per symbol while the desk
      showed nothing. `EarningsCheck` labels the source the way `SpreadSource`
      does, and an unread calendar is a rejection naming its cause: a missing
      key is a minute of config, a vendor outage clears itself, and behind one
      code they would be one number on the funnel
- [x] Approval re-derives the whole risk context instead of re-running the
      engine against the portfolio alone. The last gate before capital moves was
      the weakest in the pipeline: passing no context, it silently skipped
      correlation, sector exposure, unresolved broker state and the calendar,
      so a card drawn an hour earlier was waved through on numbers alone. It now
      rebuilds, which is also what lets a print that appeared while the card
      waited stop the approval. A context that cannot be built is a rejection,
      not a pass. Market regime is the one fact still carried from the scan —
      re-deriving it means a second round of vendor calls on a human's click
- [x] The earnings windows are counted from the exchange's day (`core.clock`),
      not the server's. `datetime.now(UTC).date()` is a day ahead of New York
      from 20:00 ET, and while that only made a print look nearer than it was in
      the engine, the provider's next/last split is not conservative in the same
      direction: it filed a print scheduled for tonight under `last_date`, whose
      window is one day rather than three, so an evening scan saw no upcoming
      print at all. Nothing traded on it: entries are RTH-only, the two
      calendars agree during RTH, and the 6h cache expires before the next open.
      That is three coincidences holding up a wrong number, and none of them is
      a rule anyone wrote down
- [x] The liquidity gate is armed on the path that actually trades. It was
      written, fail-closed, covered by `test_gates.py` and listed here as
      enforced — and it had never run on the desk. `market_data` is an optional
      constructor argument, both routes that authorize a trade omitted it, and
      the gate answered "no failure" when it had no port, which the caller reads
      as a pass. So every approve skipped spread, average dollar volume, the
      price floor, participation and expected slippage, and nothing upstream
      checks any of them. The wiring lives in `api/deps.py` now, a missing port
      is `MARKET_DATA_NOT_CONFIGURED` rather than silence, and a source-scanning
      test stops a route from building its own service again. Twenty-five tests
      had to be given a data port to keep passing, which is the measure of how
      long the suite had been agreeing with the hole
- [x] IBKR instrument resolution to an explicit `conId`, with ambiguity, wrong
      currency, unsupported type and OTC all rejected
- [x] Production-shaped IBKR transport over `ib_async`, connection states,
      bounded reconnect, and PAPER/LIVE separation enforced at construction
- [x] `permId` / `orderRef` correlation so a durable intent survives a reconnect
- [x] Failure matrix — `docs/architecture/execution-failure-matrix.md`
- [x] A stale schema stops the API instead of failing one query at a time.
      `create_all` adds tables but never alters one, so migration `0005` left an
      existing journal without `order_intents.purpose` and reconciliation died
      on every desk poll behind a warning. `init_db` now compares columns, not
      just tables, on SQLite and Postgres alike
- [x] Failed reconciliation is visible on the desk, not only in the log. While
      it is down the numbers shown are the local book's opinion rather than
      broker truth, and that difference is what the operator needs to see
- [x] The desk event stream expires on its own. A graceful shutdown drains
      in-flight responses *before* the lifespan hook runs, so an endless SSE
      stream held the server open for as long as a browser tab stayed open
- [x] Every agent taking part in the current pass reads as working, border and
      label together. The board is sampled every five seconds while most stages
      finish in far less — scoring structure never awaits at all — so a literal
      `status == "working"` was unobservable for the analysts and made a busy
      pipeline look like one agent working alone. The snapshot now reports
      `active`, "did work inside a window longer than the poll interval", which
      decays on its own once the pass moves on. The desk derives one state from
      it for the label, the dot and the border, because a running border around
      a row that says "Done" asserts two contradictory things at once. Only
      `done` is upgraded by activity: `idle` and `error` are the agent saying
      outright that it is not working — a scanner parked on a full queue, a call
      that failed — and a window must never talk over that
- [x] The header carries the two conditions that decide whether a click can do
      anything, in place of a read-only search box that did nothing. New entries
      are RTH-only, so a queue of proposals on a Sunday is a queue nobody can
      act on: `/desk` now reports the session phase, its consequence for
      entries, and the exchange clock it was judged against — all from
      `session_hours`, so the header cannot disagree with the gate, and pinned
      by a test that compares it against `check_rth` directly. The kill switch
      moved into shared state for the same reason: it was readable only on
      `/settings`, so the way to discover trading was blocked was to try to
      trade. Both distinguish "not read yet" from "could not be read", and
      neither is drawn as the safe value on the strength of a request that never
      came back
- [x] One walker over the universe, always. `POST /scanner/run` ran a cycle
      inline while the scanner loop was running its own, and the desk requested
      one on every mount, so each caller became another walker over the same
      `STATUS.funnel` and another multiple of the market-data request rate.
      Live symptoms: `scanned 191/60` against a universe of 60, one summary
      claiming `scanned 0` and `outranked 5` together, a symbol analysed four
      times in twenty seconds, 175 `429`s in a cycle. The endpoint now wakes the
      single loop instead of starting a pass, `run_scan_cycle` refuses re-entry
      outright, and mounting the desk no longer asks for a scan — page loads
      must not drive scan cadence. `SCAN_PACING_SECONDS` paces one walker and
      says nothing about how many exist, which is why the guard is the fix and
      not a longer delay
- [ ] Verified against a real IBKR Paper account — **blocked: no IB Gateway
      session or credentials in this environment**

## Stage 8 — Strategy as a first-class object
- [ ] Strategy registry with versioned, immutable strategy definitions
- [ ] Promotion gate: proposal → backtest → out-of-sample → walk-forward →
      paper → human approval → production
- [ ] Complete multi-timeframe confluence (1D/4H/1H/15m); config already
      declares four timeframes while the scanner runs two
- [ ] Price action: breakout, retest, gap detectors
- [ ] Chart patterns beyond double top/bottom

## Stage 9 — Evidence layers
- [ ] Fundamental agent + SEC EDGAR (10-K / 10-Q / 8-K)
- [ ] News event taxonomy (EARNINGS, FDA, MERGER, LEGAL, …) replacing headline
      sentiment
- [ ] Macro agent producing an explicit RISK_ON / RISK_OFF regime
- [ ] Market memory and strategy memory (trade memory exists as the journal)

## Stage 10 — Agent governance
Required before any agent actually calls an LLM.

- [ ] Runtime tool allowlist per agent, enforced not documented
- [ ] AI observability: model, prompt version, tokens, latency, cost,
      confidence, errors per run

## Stage 11 — Autopilot
- [ ] Enable per approved strategy version only, never globally
- [ ] Autopilot decisions remain subject to Risk and Liquidity unchanged

## Stage 12 — Live capital
- [ ] Market-data licensing metadata
- [ ] Small live → confirmation live → limited autopilot, gated on results

---

## IBKR adapter (decided 2026-08-30, schedule open)

IBKR is now the execution broker, so the adapter is no longer end-of-roadmap
work. It should land once Stage 7 defines the order state machine, since that
machine must be modelled on IBKR semantics rather than retrofitted from
Alpaca's.

- [x] `IBKRBroker` behind `BrokerPort` with an IB→domain mapping layer
- [x] Passes the shared broker lifecycle contract suite (`tests/contract/`)
      against a fake transport, alongside Alpaca
- [x] `assert_paper_only()` continues to apply — IBKR Paper is still paper
- [x] Instrument resolution to `conId` before any order leaves the adapter
- [x] TWS/Gateway session handling implemented (`broker/ibkr/live_transport.py`,
      `ib_async`) — written against the documented API, never run against a
      gateway
- [ ] Verified against a real IBKR Paper account
- [ ] Only then may Alpaca be retired
