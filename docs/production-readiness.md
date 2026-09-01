# Production readiness

Status of the hardening program, written to be read by someone deciding whether
to put money behind this. Every line is a claim about code that exists, not
about code that is planned, and the absences are listed as prominently as the
guarantees — a readiness document that only lists strengths is a marketing page.

**Verdict: ready for IBKR paper once a real Gateway session has been run.
Not ready for live capital.** The reasons are in
[Blocking for live capital](#blocking-for-live-capital), and none of them are
about the execution core.

Quality gate at the time of writing: 699 tests passing, `ruff` clean,
`mypy --strict` clean across the capital path (`core`, `risk`, `broker`,
`quant`, `trading`).

---

## What is proven

These are the claims backed by tests that fail when the behaviour is removed.
Each was mutation-checked: the guarantee was deliberately broken and the named
test caught it.

### Execution core

| Guarantee | Where it is enforced | Proof |
| --- | --- | --- |
| A durable `OrderIntent` precedes every broker contact | `trading/execution.py` | `test_the_intent_is_persisted_before_the_broker_is_contacted` |
| One `idempotency_key`, at most one broker order | Unique index + intent store | `test_12_a_lost_reply_does_not_produce_a_second_order` |
| A lost reply is resolved by reading the venue, never by re-sending | `_recover_entry`, `_recover_protective_stop` | `test_17`, `test_24a` |
| A restart mid-exit does not sell twice | Intent adoption by `client_order_id` | `test_18_a_restart_during_an_exit_does_not_sell_twice` |
| A partial fill is a position, protected for exactly the filled quantity | `trading/execution.py` | `test_13`, `test_partial_fill_on_timeout_is_protected_not_abandoned` |
| A partial exit resizes the stop to the remainder | `trading/exits.py` | `test_21`, `test_a_partial_exit_resizes_protection_to_the_remainder` |
| A position that cannot be protected is flattened, not left naked | Emergency close | `test_15`, `test_15b_the_emergency_exit_is_durable` |
| `UNKNOWN` blocks new entries until reconciliation clears it | Entry gates | `test_the_risk_engine_rejects_a_symbol_with_unresolved_broker_state` |
| One open position per symbol | Refused three times: entry, ledger, **partial unique index** | `test_one_open_position_per_symbol.py` |
| Two reconciliation passes place one stop | Single-flight supervisor | `test_24_two_reconciliation_passes_place_one_protective_stop` |
| Protection is sized from the smaller of book and broker | `trading/reconcile.py` | `test_21b_protection_is_never_larger_than_what_the_venue_holds` |

### Gates that fail closed

Each of these refuses on missing data rather than treating absence as a pass —
the failure mode that let the liquidity gate be written, tested, documented and
never actually run.

- Stale or missing quote → `LiquidityGateRejected`, and a modeled spread can
  never satisfy a live gate.
- No market-data port → `MARKET_DATA_NOT_CONFIGURED`, not a skip. The service
  must be built through `api.deps.build_execution_service`; a source-scanning
  test refuses any route that constructs its own.
- Unreadable earnings calendar → `EARNINGS_CALENDAR_UNAVAILABLE`, not a clear
  calendar. Only `require_earnings_check=false` passes, and it is recorded.
- Reconciliation stale or never run → `RECONCILIATION_STALE` /
  `RECONCILIATION_NEVER_RAN`. Exits and reconciliation are never gated on it.
- Stale bars → `STALE_BARS`, on every timeframe and at both ends: the daily
  series the liquidity gate measures with, and the intraday series the strategy
  prices from. A feed that stopped a week ago is an error, not a pass — and a
  vendor that serves the oldest page of a window is a stopped feed wearing a
  full response.
- Non-equity, non-USD or OTC instrument → `INSTRUMENT_NOT_ELIGIBLE`.
- Calendar reasoning uses `core.clock.market_date`, never `datetime.now(UTC)`,
  which runs a day ahead from 20:00 ET.

- Unread headlines → `NEWS_NOT_CONFIGURED` / `NEWS_UNAVAILABLE` /
  `NEWS_UNVERIFIED`. The strategy vetoes on negative sentiment, so a feed nobody
  could read is a veto that cannot fire, not a clean bill of health.

### Credentials stay out of logs

Vendor keys travel in headers, not query strings, so they are not in a URL to be
carried into an exception message. Independently of that, `core.redaction`
scrubs both by known value and by shape inside `BOARD.log`, `BOARD.set_agent`
and both audit sinks — at the sink rather than the call site, because the leak
this replaced arrived through a call site nobody had audited.

### Kill switch

Refuses **new exposure**; never the defence of exposure already taken.
Protective stops, emergency closes, operator sells and reconciliation all
continue while halted. Pinned by
`tests/integration/test_kill_switch_protects_what_is_open.py`, which asserts the
entry refusal in the same file, so relaxing risk reduction by weakening the
entry gate fails the suite.

### Refusals at boot

| Condition | Behaviour |
| --- | --- |
| More than one API worker | Refuses, naming the guarantees that hold only per process |
| Schema behind the models, including a missing unique index | Refuses, naming what is missing |
| Live broker environment | Refuses; V1 is paper-only |
| `TRAIDO_TRADING_MODE=autopilot` | Refuses, because autopilot is not implemented |

### Long-only, unlevered, equities only

Enforced in code rather than convention: the candidate schema rejects non-BUY,
the risk engine refuses to construct with `allow_leverage`, `allow_short` or
`allow_options`, and sizing is capped by cash rather than buying power.

---

## What is partial

Honest middles — the mechanism exists but does not cover what the phase asked
for.

| Area | What exists | What is missing |
| --- | --- | --- |
| Scanner funnel | Nine counters, `passed == opportunities + outranked` | Symbols outside the rotating slice land in no bucket; no test that the funnel sums to `scanned` |
| Ranking | Sorted by `(confidence, risk_reward)` | No final tie-break on symbol, so equal pairs fall back to traversal order |
| Scan context | Shares broker, market data and portfolio for a cycle | No `scan_id`, no timestamp, no regime or capacity snapshot |
| Scheduler | Sleep-after-finish, overlap-guarded | No `scheduled_at`, no duration, no overrun policy |
| Concurrency | Sequential with 0.4s pacing | No semaphore, no per-vendor limits, no scan-level timeout |
| Data provenance | `ts` and `source` on bars and quotes | No `received_at`, `age` or `quality` on the schema; age is computed at the gate and discarded |
| Bar freshness | `check_bar_freshness` on every timeframe, at the gate and at the scan | The five-day bound is sized for the longest market closure, so it is loose for an hourly series: a feed three days behind still passes |
| Audit trail | ~60 event types, append-only, `pipeline_run_id` correlated | Gate results are stored on **rejection** only, so a successful entry cannot be replayed with its measured spread and freshness |
| Backtest bias | Look-ahead prevented by construction | No survivorship handling, no split adjustment (bars fetched `adjustment=raw`), no delistings |
| Backtest realism | Commissions, spread and slippage on every fill | No partial fills, no latency |
| Out-of-sample | Train/test split, walk-forward, WFE | No third validation fold; no promotion gate |
| Benchmarking | SPY buy-and-hold in the evaluation service | Engine summary leaves benchmark fields `None`; not in the review agent |
| Regime | Classified and used to block entries | The live journal's `market_regime` column is never populated |
| Readiness probe | Database hard-fails; broker and market data degrade | Reconciliation age, UNKNOWN intent count and unverified protection are not in the probe |
| Alerting | Telegram on new opportunity and kill-switch toggle | Nothing alerts on broker disconnect, UNKNOWN intent, unverified protection, stale reconciliation or emergency exit; `send_risk_halt` has no callers |

---

## What is absent

Named plainly, because a gap that is described as partial is a gap that gets
forgotten.

- **Metrics.** No Prometheus, OpenTelemetry, statsd or `/metrics`. No counters
  for scan duration, gate outcomes, order latency, fill latency, unknown
  intents or reconciliation age. Operating this desk means reading the audit
  log and the dashboard, and nothing will page anybody.
- **Strategy versioning as an entity.** `STRATEGY_VERSION` is a string constant
  in source. There is no registry, no parameter hash, no `approved_at`, and no
  promotion pipeline — so "which version placed this trade" is answerable, but
  "what exactly was that version" is answerable only by reading git.
- **Corporate actions and halts.** No handling of splits, symbol changes,
  delistings, dividends or a halted market anywhere in the runtime. A split
  overnight moves a stop to a price that is no longer meaningful.
- **Limit-change auditing.** Limits load once from JSON at boot. There is no
  runtime mutation path, which is safe, but also no record if the file changes
  between restarts.
- **Post-rounding risk recheck.** Sizing rounds quantity down to a whole share
  after the risk engine has approved, and the result is not re-validated against
  the limit. Rounding down can only reduce risk, so this is conservative — but
  it is unasserted, and on a high-priced share the discarded fraction is a
  larger share of the position than it looks.
- **A real IBKR Gateway session.** The adapter is complete and contract-tested
  against a fake transport; `ib_async` is an optional extra that is not
  installed and no test imports the live transport. Nothing here has spoken to
  IB.
- **Autopilot.** Not implemented. The desk now refuses to start in that mode
  rather than pretending.
- **LLM on the decision path.** No agent calls a model; the adapter has zero
  callers. The capital-safety tests that assert "no LLM reaches risk" are
  therefore true but vacuous, and will need to be re-proven when one is wired.

---

## Blocking for live capital

In order. The first two are what separate "paper-ready" from "proven".

1. **Run a real IBKR Paper session.** Install the extra, connect
   `IBKRLiveTransport` to a Gateway, and walk the full lifecycle: entry,
   protection, partial fill, exit, restart, reconciliation. Everything IBKR is
   currently proven against a fake of our own design, which cannot surprise us.
2. **Alert on the states that already stop trading.** Stale reconciliation,
   UNKNOWN intents and unverified protection all correctly refuse new exposure
   and are all invisible unless somebody is looking at the dashboard. A desk
   that halts silently is a desk that stays halted.
3. **Split and corporate-action handling.** At minimum, refuse to hold through
   a known split until stops are adjusted, in the same spirit as the earnings
   gate.
4. **Metrics and a paging path**, so the failure matrix has an audience.
5. **Strategy registry and a promotion gate**, so a change of parameters is an
   event with a record rather than a commit.

Reconciliation age, protection status and broker connection state should move
into `/health/ready` as part of item 2 — they are the states an orchestrator
should see.

---

## Cross-references

- `docs/architecture/execution-failure-matrix.md` — behaviour per failure,
  changed in the same commit as the behaviour.
- `docs/architecture/gap-register.md` — the numbered findings and their status.
- `docs/architecture/runtime-path-audit.md` — the original defect snapshot.
- `docs/architecture/vendor-lock.md` — vendor status, including the unproven
  IBKR row.
