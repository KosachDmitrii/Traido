# Execution failure matrix

Every row is a way the broker interaction can go wrong, and the one response
Traido is allowed to have. The point of writing them down is that the choices
are then reviewable: each row says what the local state becomes, what happens,
what is audited, whether trading is blocked, and how the situation ends.

Two rules generate most of the table:

1. **A rejection is not a timeout.** The broker answering "no" means no order
   exists and a retry is safe. Silence means an order may be live, and a retry
   is how a duplicate position is created.
2. **Absence is not evidence.** An order missing from the open-order book has
   not been proven never to have existed — a filled order is not open either.
3. **An unread check is not a passed check.** A spread nobody could measure and
   an earnings calendar nobody could fetch are both refusals, not clearances.
   Where a check cannot run, the entry does not run either.

`UNKNOWN` is the state for "broker truth is unresolved". It is not a failure
and not a success; it blocks conflicting action on the symbol until
reconciliation settles it.

---

## ENTRY

| Failure | Local state | Action | Audit event | Trading blocked | Recovery |
| --- | --- | --- | --- | --- | --- |
| Timeout before acknowledgement | `SUBMITTING` → `UNKNOWN` | No retry. Card stays claimed. | `EntryStateUnknown` | Symbol blocked for new entries | Reconciliation locates the order by `client_order_id`, or it stays `UNKNOWN` |
| Broker rejects | `SUBMITTING` → `REJECTED` | Card released to the queue | `OrderRejected`, `EntryOrderRejected` | No | Terminal. A later approval opens a new intent |
| Timeout after acknowledgement, nothing filled | `SUBMITTED` → `CANCEL_PENDING` → `CANCELED` | Cancel, re-read, release card | `OrderCancelRequested`, `OrderCancelled` | No | Terminal |
| Timeout after acknowledgement, partly filled | → `PARTIALLY_FILLED` | Cancel the remainder, protect the shares that filled | `OrderPartiallyFilled` | Until protection is installed | Protective stop sized to the actual fill |
| Cancel succeeds but the order cannot be re-read | → `UNKNOWN` | Card stays claimed | `EntryStateUnknown` | Symbol blocked | Reconciliation |
| Protective stop fails after a fill | `PARTIALLY_FILLED`/fill recorded | Emergency close the position | `StopOrderFailed`, `EmergencyCloseTriggered` | Symbol blocked until the flatten is confirmed | Confirmed flatten → `FILLED`; unconfirmed → `UNKNOWN` |
| Process restart mid-entry | Whatever was persisted | Nothing is re-sent | — | Unresolved intents block the symbol | Reconciliation reads the broker and resolves |
| Broker unreachable | `UNKNOWN` | No retry | `EntryStateUnknown` | Symbol blocked | Reconciliation once the link returns |
| Broker not `READY` | Never created | Entry refused before any intent exists | `LiquidityGateRejected`-style gate rejection | Yes, all new entries | Reconnect |
| No live quote for the spread check | Never created | Entry refused | `LiquidityGateRejected` (`LIVE_QUOTE_REQUIRED`) | That entry only | Quote feed returns |
| No market-data port on the service | Never created | Entry refused — the gate cannot measure spread, volume, price or participation, so none of them is checked | `LiquidityGateRejected` (`MARKET_DATA_NOT_CONFIGURED`) | Yes, all new entries | Build the service via `api.deps.build_execution_service`; this is wiring, and it does not clear on its own |
| No earnings calendar key | Never created | Entry refused at risk, before a proposal is offered | `RiskRejectOnApprove` / funnel `EARNINGS_CALENDAR_NOT_CONFIGURED` | Yes, all new entries | Configure `FINNHUB_API_KEY`, or accept the risk explicitly via `require_earnings_check=false` |
| Earnings calendar lookup fails | Never created | Entry refused | funnel `EARNINGS_CALENDAR_UNAVAILABLE` | That symbol, until the vendor answers | Vendor recovers; the result is cached 6h |
| Reconciliation has never succeeded | Never created | Entry refused — broker truth has never been established this run | gate rejection `RECONCILIATION_NEVER_RAN` | Yes, all new entries | The background loop's first successful pass |
| Last reconciliation older than the threshold | Never created | Entry refused | gate rejection `RECONCILIATION_STALE` | Yes, all new entries | Next successful pass. Exits and reconciliation are **not** gated |
| Symbol is not a plain US-listed equity in USD | Never created | Entry refused before sizing | gate `instrument`: `SYMBOL_LOOKS_OTC`, `SECURITY_TYPE_NOT_EQUITY`, `CURRENCY_NOT_USD`, `SYMBOL_NOT_ALLOWED` | That symbol | Add it to `allowed_symbols` deliberately, or never |
| Daily bars behind the liquidity maths are stale | Never created | Entry refused — ADV and participation would be computed from an old tape | gate `data_freshness`: `STALE_BARS` / `NO_BARS` | That symbol | Fresh bars from the vendor |
| Two entries for one symbol, from two processes | Second never opens | The database refuses the second open row; the caller sees `DuplicateOpenPosition` | `EntryBlockedByOpenPosition` | That symbol | The first position closes |
| Approved size rounds down to less than one share | Never created | Entry refused before any order is built — orders are whole shares, so the alternative is a zero-quantity order | `EntrySizeBelowOneShare`, refusal `SIZE_BELOW_ONE_SHARE` | That symbol | A larger book, or a cheaper share |
| No live book to price the entry against | Never created | Entry refused — the limit crosses the offer, so without an offer there is no order to build | `LiquidityGateRejected` (`LIVE_QUOTE_REQUIRED` / `QUOTE_STALE`) | That entry only | Quote feed returns |
| Market moved above the card's target, or below its stop, before the click | Never created | Entry refused — buying above the target, or under the stop, is not the trade the card described | `LiquidityGateRejected` (`PRICE_MOVED_PAST_SETUP`) | That entry only | A later scan draws a card at the new price |
| Crossing the spread makes the position exceed a risk limit | Never created | Entry refused at the re-check, which is run at the executable price rather than the card's | `RiskRejectOnApprove` with `repriced_entry` | That symbol | A price that fits the limits |
| Market ran up far enough that the entry costs a quarter of the planned risk | Never created | Entry refused — the stop and target do not move with the price, so paying up shortens the reward and lengthens the risk at once; past this line it is no longer the setup that was analysed | `LiquidityGateRejected` (`ENTRY_TOO_FAR_ABOVE_CARD`), carrying `paid_above_card` and `repriced_risk_reward` | That entry only | A later scan draws a card at the new price |
| Spread too wide to price against | Never created | Entry refused before the limit is derived rather than after, so the reason names the book and not the drift it caused | `LiquidityGateRejected` (`SPREAD_TOO_WIDE`) | That entry only | The book tightens |
| Candidate sector could not be established | Never created | Entry refused — `"unknown"` is not a sector, and treating it as one let a name bypass its real sector's cap | `RiskReject` (`SECTOR_UNCLASSIFIED` / `SECTOR_NOT_CONFIGURED` / `SECTOR_UNAVAILABLE` / `SECTOR_UNVERIFIED`) | That symbol | Map it in `configs/universe.json`, configure `FINNHUB_API_KEY` so `profile2` can classify names outside the file, or accept via `require_sector_check=false` |
| Candidate has a target price but no target model or reachability | Never created; any older card is `DISCARDED` | Refuse publication because final admission cannot reproduce the `TargetPlan`; reconciliation removes already-persisted incomplete cards | `OpportunityDiscarded` / refusal `TARGET_PLAN_REQUIRED` | That card | A fresh scan rebuilds and persists the complete target plan |
| Standing BUY card whose live book no longer clears entry geometry | Card stays `AWAITING_CONFIRMATION` | Desk marks `viability.buyable=false` and disables BUY; card is **not** withdrawn (spread and drift come back) | Desk field `viability.state` ∈ {`wide`,`drifted`,`past_setup`,`unverified`} | That card's BUY only | Book tightens / price returns / next scan redraws levels |
| Auto Trigger pre-submit refusal (stale quote, unread portfolio, reconciliation, RTH/liquidity, wide spread, chase, RR drop) | Card stays `AWAITING_CONFIRMATION` | Retry with backoff. Never `SKIPPED` or `DISCARDED` | `AutoTriggerApproveDeferred` | That card until retry | Book tightens / data or broker returns |
| Auto Trigger terminal reject (regime not tradable, thesis, geometry) | `DISCARDED` | No retry | `OpportunityDiscarded` | That card | A later scan may draw a new card |
| Auto Trigger error after a possible broker submit | Unchanged (`UNKNOWN` / `APPROVING`) | No second `decide()`. Reconciliation only | `AutoTriggerStateUnknown` | Symbol blocked | Reconciliation |
| Entry fill wait ends, cancel/re-read proves zero shares filled | Card returns to `AWAITING_CONFIRMATION` | Retry only after operational backoff; this is not `UNKNOWN` because broker truth was established | `EntryFillFailed`, `AutoTriggerApproveDeferred` | That card until retry | Broker/link recovers |
| Watch lacks a mark, enough H1 bars, or top-of-book | `BLOCKED_DATA` | Persist the block and retry the full revalidation; never leave a silent `TRIGGERED` card | `EntryWatchDataBlocked` + DecisionOutcome | That watch | Required market data returns |

A standing proposal and a live entry are different questions. `withdraw_unactionable`
removes only durable facts (TTL, open position, irreproducible target plan). Transient book conditions are
previewed on the card via `trading.viability` — the same geometry decide
enforces — so the operator does not have to press BUY to learn the book moved.

## Scanner and watch observations

- BUY previews and final admission share `candidate_entry_zone` and
  `entry_allowed_for_setup_type`, including snapshot precedence and ask-based
  ATR tolerance. Outside-zone cards remain visible but cannot be clicked;
  incomplete entry evidence is distinct from an unverified quote. Preview
  never replaces the final fresh admission check.

- Alpaca account summary has no verified weekly baseline/high-water history.
  Weekly P&L and drawdown are therefore nullable, not fabricated zeroes. New
  entries fail closed with `WEEKLY_PNL_UNAVAILABLE` /
  `PORTFOLIO_DRAWDOWN_UNAVAILABLE`. Do not deploy this as a complete accounting
  solution: a verified, cash-flow-adjusted account history source is still
  required to make those checks operational. Exit paths are unchanged.

- Budget rotation reserves half of each capped eligible shortlist for ranking
  and half for least-recently selected names. Eligibility gates are unchanged.
  `ScannerSelectionV1` audit rows persist exchange-day history. Scheduled deep
  work and completed pipeline calls are counted separately; completion does not
  imply BUY or successful provider data. The desk API exposes session coverage.

- Unexpected outer scanner failures are retried after the existing paused retry
  delay. Cancellation still stops the worker; no parallel replacement is launched.
  Readiness reports a degraded scanner while it is missing or recovering.
  `TRAIDO_SCANNER_CYCLE_TIMEOUT_SECONDS` bounds a cycle (default 1800 seconds);
  timeout awaits cancellation before retrying, rather than launching a second
  walker alongside unfinished work. It cannot recover a blocked Python event loop.
- A newer last trade never updates the source timestamp of bid/ask. Stale book
  data remains stale for admission, even when a fresh last price is displayed.
- Zone transitions use the observation preceding the UI mark refresh. Repeated
  observations inside the same zone are not additional touches.
- Triggered watch scoring receives a freshly assessed market; publication still
  independently checks its market gate before risk evaluation.

## EXIT

| Failure | Local state | Action | Audit event | Trading blocked | Recovery |
| --- | --- | --- | --- | --- | --- |
| Timeout before acknowledgement | `SUBMITTING` → `UNKNOWN` | No retry. Card stays claimed. | `ExitStateUnknown` | New exits for this position | Reconciliation locates by `client_order_id`, or stays `UNKNOWN` |
| Broker rejects | → `REJECTED` | Card released; protection restored | `ExitRejected` | No | Terminal; a new SELL opens a new intent |
| Timeout after acknowledgement, nothing filled | → `CANCEL_PENDING` → `CANCELED` | Cancel, re-read, re-install the stop that was cancelled for the sale | `ExitCancelRequested`, `ExitCancelled`, `ProtectionResized` | No | Position intact and protected |
| Partial fill | → `PARTIALLY_FILLED` → `CANCELED` | Ledger reduced by the fill only; protection resized to the remainder; card released | `ExitPartiallyFilled`, `ProtectionResized` | No | Remainder is a normal open position |
| Broker cancels after a partial fill | → `PARTIALLY_FILLED` | Same as above. The shares that sold stay sold | `ExitPartiallyFilled` | No | Remainder preserved |
| Duplicate SELL from the UI or a callback retry | Unchanged | Existing intent resumed, never re-sent | `DuplicateOrderPrevented` | No | Adopts the first order |
| Process restart mid-exit | Whatever was persisted | Nothing is re-sent | — | Unresolved exit blocks new exits | Reconciliation applies any fill to the ledger exactly once |
| Broker unreachable | → `UNKNOWN` | Card stays claimed | `ExitStateUnknown` (`severity: critical`) | New exits for this position | Reconciliation |
| Broker not `READY` | Never created | Discretionary exit refused | `ExitBlockedByBrokerState` | Discretionary exits | Reconnect. Emergency exits are not gated |
| Emergency exit already in flight | Never created | Discretionary exit refused | `ExitBlockedByUnresolvedState` | This position | Emergency exit resolves first |
| Operator asks to close a symbol the venue does not hold | Never created | Refused with `no_open_position`, 404. Nothing is sent — a close is not an invitation to go short | — | No | Nothing to recover |
| Operator asks to close twice | Second never created | The second click meets the state machine: the card is no longer `AWAITING`, or the venue is already flat | `PositionCloseRequested` then the ordinary exit refusal | No | One sell, one journal row |
| Operator closes while the desk is halted | Normal exit | Allowed. The switch refuses new exposure, never the shedding of it | `PositionCloseRequested` | No | — |

## EMERGENCY CLOSE

| Failure | Local state | Action | Audit event | Trading blocked | Recovery |
| --- | --- | --- | --- | --- | --- |
| Triggered twice by two workers | One intent | Second trigger resumes the first order | `DuplicateOrderPrevented` | No | Single flatten |
| Triggered again after a completed flatten | Intent already `FILLED` | Refuses to send. Those shares are gone | `DuplicateOrderPrevented` | No | No short |
| Broker rejects the flatten | → `REJECTED` | Reported as not flat | `EmergencyFlattenFailed` | Position remains unprotected and blocked | Escalation |
| Timeout after the broker accepted | → `UNKNOWN` | Cancel and re-read; absorb any partial fill | `EmergencyFlattenUnconfirmed`, `EmergencyExitUnknown` (`critical`) | Yes | Reconciliation |
| Partial flatten | → `PARTIALLY_FILLED` | Ledger reduced by the fill; **not** reported as safe | `EmergencyExitUnknown` (`critical`) | Yes | Remaining exposure escalated |
| Process restart mid-flatten | Persisted intent | Nothing re-sent | — | Yes | Reconciliation resolves against the broker |

## PROTECTION

| Failure | Local state | Action | Audit event | Trading blocked | Recovery |
| --- | --- | --- | --- | --- | --- |
| Broker rejects the stop after an entry fill | Fill recorded | Emergency close. A rejection means no stop exists and none will | `StopOrderFailed`, `EmergencyCloseTriggered` | Until flat is confirmed | Confirmed flatten, or `UNKNOWN` |
| Reply lost while placing the stop | Protective intent → `UNKNOWN` | **Not** flattened on the spot. The order is looked up by its `client_order_id`; a live one is adopted, and only a genuinely absent one leads to a flatten | `ProtectiveOrderRecovered`, or `StopOrderFailed` then `EmergencyCloseTriggered` | Until protection is verified | Adoption, or flatten |
| Two passes try to protect one position | One intent | The second resumes the first, keyed `protection:{position_id}:{generation}` | `DuplicateOrderPrevented` | No | Exactly one stop |
| A resting SELL beyond what the venue holds | Position open | Cancelled, newest first, until resting protection no longer exceeds the holding | `ExcessProtectionDetected`, `OrderCancelled` | No | Protection ≤ holding |
| Stop has disappeared at the broker | Position open | Re-install at the position's stop price | `ProtectiveOrderMissing`, `ProtectiveOrderRecovered` | No | New stop id recorded |
| Stop quantity larger than the position | Position open | Cancel and replace at the true size | `ProtectionQuantityMismatch` (`critical`), `ProtectionResized` | No | Stop equals position |
| Stop quantity smaller than the position | Position open | Same repair, lower severity | `ProtectionQuantityMismatch` (`warning`), `ProtectionResized` | No | Stop equals position |
| Book and venue disagree about the size | Position open | Protection is sized to the **smaller** of the two, never the local number | `ProtectionQuantityMismatch`, `ProtectionResized` | The quantity mismatch blocks the symbol separately | Stop never covers shares the venue does not hold |
| Partial exit requires a resize | Position reduced | Resize to the remaining quantity | `ProtectionResizeRequested`, `ProtectionResized` | No | Stop equals remainder |
| Resize fails | Remainder unprotected | Emergency close the remainder | `ProtectionResizeFailed` | Until flat is confirmed | Flatten, or `UNKNOWN` |
| Broker open orders unreadable | Position open, protection **unverified** | Nothing re-placed; every open position named individually | `ProtectionUnverified` (`critical`) | No new entries while the link is not `READY` | Next successful read verifies or repairs |
| Stop resting and correctly sized, but simulated by the venue | Position open | Nothing — this check cannot detect it | — | No | Emergency close remains the backstop; see `vendor-lock.md` |

A protective order is external state. Traido verifies that it **exists and is
correctly sized**, every pass, by reading it back from the broker. It cannot
verify that the venue will fire it: IB serves some stops natively and simulates
others, with trigger behaviour that depends on product, venue and session. No
safety invariant here assumes otherwise — a resting stop is evidence, and the
emergency-close path is what makes the invariant hold when the evidence turns
out to be wrong.

## RECONCILIATION

| Discrepancy | Local state | Action | Audit event | Trading blocked | Recovery |
| --- | --- | --- | --- | --- | --- |
| Local quantity > broker, explained by recorded exit fills | Position open | Local reduced to broker truth | `PositionQuantityReconciled` | No | Book agrees with broker |
| Local quantity > broker, unexplained | Position open | Local left alone; symbol blocked | `PositionQuantityMismatch` (`critical`) | Yes | Human investigation |
| Local quantity < broker | Position open | Treated as unexplained | `PositionQuantityMismatch` (`critical`) | Yes | Human investigation |
| Broker order missing for an unresolved intent | → `UNKNOWN` | No guessing | `EntryStateUnknown` / `ExitStateUnknown` | Symbol blocked | Later pass, or human |
| Broker position with no ledger row | — | Synthetic `UNKNOWN` intent blocks the symbol | `OrphanBrokerPosition` | Yes | Block lifts when the position is gone |
| Ledger open, broker flat | Position open | Journalled as closed by stop or external action | `PositionReconciledClosed` | No | Journal squared |
| Local says closed, broker still holds it | — | Never accepted. Treated as an orphan | `OrphanBrokerPosition` | Yes | Investigation |
| Exit filled while we were not looking | → `FILLED`/`PARTIALLY_FILLED` | Fill applied to the ledger exactly once | `ExitFillReconciled` | No | `applied_exit_qty` makes repeat passes no-ops |
| Resting exit keeps filling (40 → 70), status unchanged | stays `PARTIALLY_FILLED` | Only the difference is absorbed each pass | `ExitFillReconciled` | No | Book tracks the order while it works |
| Crash between the ledger write and the intent update | intent looks unfinished over a correct book | Fill recognised as already absorbed | `ExitFillReconciled` (no-op) | No | `applied_exit_qty`, not intent status, is the record |

---

| Two callers ask for a fresh pass at once | — | One pass runs; both receive its result | — | No | Single-flight, so a race cannot be created by watching |
| A pass raises | Last good success retained, `ok=false` with the reason | Recorded and rendered on the desk banner, never swallowed; the loop survives to the next tick | — (status, not an audit event) | Entries block once the last success ages past the threshold | Next pass |
| Nobody is watching the dashboard | — | The background loop runs anyway | — | No | Reconciliation is a control loop, not a page render |

## VENDOR CHECKS

Two vendor reads can veto an entry, and both fail closed. What matters is that
each distinguishes "read it, nothing wrong" from "could not read it" — those
arrive looking identical otherwise.

| Condition | Reason | Recovery |
| --- | --- | --- |
| No Finnhub key, earnings required | `EARNINGS_CALENDAR_NOT_CONFIGURED` | Add the key |
| Calendar unreadable | `EARNINGS_CALENDAR_UNAVAILABLE` | Clears when the vendor does |
| Caller never fetched a calendar | `EARNINGS_UNVERIFIED` | Build the context properly |
| No Finnhub key, news required | `NEWS_NOT_CONFIGURED` | Add the key |
| News endpoint down, e.g. a 503 | `NEWS_UNAVAILABLE` | Clears when the vendor does |
| Caller never fetched headlines | `NEWS_UNVERIFIED` | Build the context properly |
| Name outside `universe.json`, no Finnhub key | `SECTOR_NOT_CONFIGURED` | Add the key, or map the name in the file |
| `profile2` unreadable | `SECTOR_UNAVAILABLE` | Clears when the vendor does |
| Profile empty / industry unmapped | `SECTOR_UNCLASSIFIED` | Map in `universe.json`, or extend the industry table |
| Caller never established a sector | `SECTOR_UNVERIFIED` | Build the context properly |

A vendor failure is returned as a status, never raised. Raising took the whole
symbol's pipeline down and the scanner recorded it as `no_candidate` — the same
bucket as "there was no setup here", which is precisely the confusion the funnel
exists to prevent.

"Clears when the vendor does" is a claim about elapsed time, and it has to be
paid for in two places.

Every vendor read retries through `core.vendor_http.get_with_retry`: three
attempts, backoff doubling from 0.4s, on 5xx, 429 and transport errors. A 4xx
that is not 429 is not retried — a rejected key or an unknown symbol answers the
same way the second time, and only spends quota saying so. Failures are named by
status code (`HTTP 503`, `HTTP 401`), never by the vendor's own message, which
carries the request URL and therefore any key passed as `token=`.

Retrying a 429 is the second half of the answer; the first is not provoking one.
The market-data adapter paces every request it makes — bars, quotes, snapshots
and batches alike — against one process-wide token bucket sized to the account's
quota (`TRAIDO_MARKET_DATA_RPM`, default 180 against Alpaca's 200/min). The
bucket is per key rather than per adapter, because the scan cycle and
reconciliation each build their own and a limiter apiece would permit exactly
twice the quota. It is consulted before every attempt, retries included: pacing
only the first would retry a throttled endpoint at full speed, spending the
quota the 429 asked us to conserve.

The scanner's `deep` budget does not substitute for this. It paces *symbols*,
and one Stage 3 symbol paginates hourly bars a dozen times, so four concurrent
symbols is a burst of roughly fifty requests. A live 176-name cycle finished
`risk-passed 0 · published 0` with the desk log full of 429s against the bar
endpoint, and nothing in the funnel was wrong: the symbols had simply been
throttled out of the deep stage. That is the shape of the failure — a clean
funnel, and no proposals, for a reason the funnel does not name.

Failed calendar reads are cached separately from successful ones —
`FAILURE_TTL` of two minutes against a `CACHE_TTL` of six hours. Under one TTL a
single 503 refused the symbol every cycle until evening, long after Finnhub had
recovered; that is the failure this table used to under-describe. Short rather
than zero, so a burst of lookups for one symbol does not become a burst of
retries against a vendor already in trouble. News is not cached at all and
recovers on the next cycle by construction.

Waivers are `require_earnings_check=false` and `require_news_check=false`, and
both are recorded in `limits_applied` on every decision taken under them.

## KILL SWITCH

The switch refuses **new exposure**, never the defence of exposure already
taken. It is pressed when something has gone wrong, which is exactly when open
positions most need a stop and a way out — so a halt that also disarmed the desk
would work against the reason for pressing it.

The distinction is carried on `OrderRequest.purpose`, copied from the durable
`OrderIntent`, because the broker adapter is where the refusal lives and it
cannot otherwise tell an entry from a protective stop. The field defaults to
`ENTRY`, so an unlabelled order is treated as new exposure and refused.

| Action while halted | Allowed | Where enforced |
| --- | --- | --- |
| New entry (approve an opportunity) | **No** | `RiskEngine`, `ExecutionService.approve`, and the broker adapter |
| Scanner publishing opportunities | **No** | `agents/scanner/agent.py` |
| Protective stop placed or resized by reconciliation | **Yes** | `purpose=PROTECTIVE_EXIT` passes the adapter |
| Emergency flatten after protection failed | **Yes** | `purpose=EMERGENCY_EXIT` |
| Operator pressing sell on an exit card | **Yes** | `purpose=EXIT`; `decide` is deliberately not gated |
| Reconciliation reading broker truth | **Yes** | Never gated; reads take no risk |

Pinned by `tests/integration/test_kill_switch_protects_what_is_open.py`, which
asserts the entry refusal in the same file as the three permissions, so a change
that opened up risk reduction by weakening the entry gate fails.

## STARTUP

The desk can also be wrong before it does anything, and those failures are worth
tabulating for the same reason: each one is silent by default.

| Condition | Action | Recovery |
| --- | --- | --- |
| More than one API worker requested | Refuses to boot, naming the guarantees that hold only per process | One worker, or `TRAIDO_ALLOW_MULTI_WORKER=1` once they are cluster-safe |
| Schema behind the models — a table, a column, or a **unique index** | Refuses to boot, naming what is missing | `alembic upgrade head` |
| A local SQLite database predating a declared index | The index is created in place | — |
| That database already violates the index | Refuses to boot; nothing is dropped to make it fit | Resolve the duplicate rows by hand |
| Migration `0006` against a book with two open rows for a symbol | Refuses, listing the symbols and the resolution procedure | Reconcile each against the broker, close the losers, re-run |
| Reconciliation has not yet run | Entries refused as `RECONCILIATION_NEVER_RAN`, visibly | The loop's first pass, seconds later |
| Selected Alpaca backend has no credentials | Broker construction fails closed; API returns `503 OPERATIONAL_BLOCKED` | Configure credentials. Tests opt in explicitly with `TRAIDO_BROKER_MOCK=true`; production never falls back silently |

---

## Repeatability

Reconciliation is designed to be run on a loop. Every step keys off current
state, and exit fills are absorbed against `OrderIntent.applied_exit_qty` rather
than against intent status, so a second pass over an already-consistent book
changes nothing — and a *growing* fill under an unchanged status is still picked
up. Status is the wrong record to key on here, because the crash that matters is
exactly the one where the status update never happened.

Regressions: `test_running_reconciliation_twice_does_not_sell_the_position_twice`,
`test_a_fill_the_book_already_absorbed_is_not_absorbed_again`,
`test_an_exit_that_keeps_filling_is_absorbed_as_it_goes`.
# Alpaca Paper observed risk period (2026-09-09)

Operator-approved policy: begin a NEW observation period from current broker
equity; do not reconstruct or assert earlier weekly history. Paper-only, USD, broker-reported Alpaca account identity.

| Condition | Behaviour |
|---|---|
| No period yet | Weekly/drawdown unknown; entries blocked; GET never initializes |
| Explicit authenticated start | Requires matching verified account and fresh equity; baseline and audit committed atomically; existing exposure does not authorize orders |
| Duplicate start / restart | Same durable account key; never resets losses or high water |
| New exchange week | Last observed equity is carried as baseline, including gap movement on first read; actual baseline timestamp disclosed |
| Missing/corrupt risk store or changed account | Unknown metrics; entries blocked; portfolio remains readable for exits |
| Period suspended | Unknown metrics; entries blocked, exits/protection unaffected; Start cannot reactivate/reset |
| Funding / paper reset planned | Operator MUST suspend first; automatic cash-flow detection/adjustment is NOT implemented |

This is sampled net-liquidation accounting, not cash-flow-adjusted trading P&L
or a complete historical intraday high-water mark. Unobserved peaks cannot be
recovered from the broker. Do not fund/reset the paper account while this period
is active: unreported funding can invalidate its metrics. Suspension preserves
history; subsequent funding reconciliation/resumption requires a separate
implementation, not deleting the row. No live use is authorized.

During an active period, daily P&L also uses observed net liquidation (including
unrealized changes) instead of the old realized-only fallback. Exchange-day
and week boundaries carry the last observation; no automatic lifetime-peak
reset. Unchanged observations are checkpointed at most once per minute;
changed equity is persisted immediately on read. All changes have audit events.

Migration: `0016_risk_periods` (`alembic upgrade head`) before backend startup.
Activation after deployment: Settings → Alpaca Paper → Start risk accounting,
verify displayed account/equity and confirm. No orders are sent by activation.
The period starts when that request succeeds, not when code is deployed.


## Observed Alpaca Paper period with existing exposure

Decision: an explicitly confirmed first observed period may include existing
positions and resting orders in the broker's current USD equity.
This is an observation baseline, not reconstructed weekly performance and not
an admission or reconciliation action. It does not resolve UNKNOWN intents,
adopt external positions, place/cancel orders or reset an existing/suspended
period. Account identity and READY state are rechecked before persistence.
Existing execution/reconciliation gates remain responsible for trading access.

Desk order-read failure is exposed as `open_orders_verified=false`, never as
verified absence. Position valuation uses Alpaca current_price keyed by
account and symbol; missing/invalid prices remain unknown. Stop/target fields
remain visible as unknown when the ledger has no evidence. Scanner cycle WAIT
counts are distinguished from active plans across cycles.


## Entry fill recovery before ledger creation

Reconciliation scans durable entry intents including terminal FILLED records for
currently held, unattributed positions. It restores a row only from one matching
broker fill, the same broker account/environment, exact held quantity, and the
persisted approval/geometry. Missing or conflicting evidence remains an orphan;
no guessed targets or fresh approval are substituted. Closed entry rows are never
resurrected. The existing protective sweep runs after restoration, including on
future passes. A matching resting stop is linked, never blindly duplicated.
An executing approval is tracked process-wide and excluded from recovery until
it finishes; the tracker is deliberately empty after process restart. The
single-worker deployment invariant remains required. Recovery submits no BUY.


## Price eligibility consistency
Discovery and deep analysis share the default USD 5–10000 price band from
`universe.price_policy`. Deep analysis reports the measured daily close and
bounds on rejection; missing/non-finite prices remain rejected as unavailable.
This removes the conflicting hardcoded USD 2000 ceiling, without changing
liquidity, sizing, admission, or portfolio risk gates.

### Scanner discovery and attribution — 2026-09-09

| Condition | Behaviour | Evidence |
|---|---|---|
| Eligible universe exceeds scan budget | Full screened reference pool stays cached; half the scan budget retains ranking, half visits least-recently selected names. Persisted daily history survives restart. Market, admission and risk gates remain mandatory. | `ScannerSelectionV1`, stage `universe_selected`; session coverage |
| Broker position without local ledger row | Exclude held symbol before market/deep work; re-read positions before publication. Does not fabricate or repair the ledger. | `position_open` terminal bucket |
| Broker positions cannot be read | Stop new candidate scan/publication with operational-blocked counts; protection and reconciliation continue independently. | `broker_positions_unavailable`, scan identity |
| Deep outcome completes during the batch | Update completed and terminal counts immediately. Started and completed are separate. | Live funnel, per-symbol `ScannerTrace` |
| Admission/risk rejects or publication loses a slot | Preserve rejected admission results, show risk rejection counts and reasons, trace deep/publication outcomes with pipeline and scan IDs. | `ScannerTrace`, admission record geometry and target provenance |

Coverage counts selections, not successful market-data reads or trade permissions.
Deep completion means the pipeline returned an outcome, possibly a rejection.
Session unique counts and current pool size are different populations if eligibility
changes intraday; they are not presented as a completion percentage.

### WAIT admission versus current entry readiness (2026-09-09)

| Condition | Behaviour | Evidence |
|---|---|---|
| Bullish pullback candidate is above its allowed entry zone, but its initial label was BUY | Derive the existing zone-based WAIT plan before admission; require all stop/target/data and pre-watch risk checks. Never promote it directly to BUY. | Candidate and bundle both carry WAIT; admitted plan geometry is persisted |
| Zone-plan admission passes while candidate is waiting | Persist WAIT with admitted=false and buy_ready=false; pre-watch no longer rejects it solely as ADMISSION_NOT_WAIT. | Admission record and watch share observation-only decision |
| Current entry quality below the BUY floor, with otherwise valid setup and geometry | WAIT for improvement; the BUY floor itself remains unchanged. Invalid setup, stop, target, stale data and risk failures still block. | CANDIDATE_ENTRY_BELOW_FLOOR; regression at all slider levels |
| Pre-watch eligibility rejects | Copy final reason codes into the pipeline outcome and scanner counters. | Deep trace contains admission decision, full reason codes and candidate decision |

These are observation-policy corrections, not evidence of profitability or a
guaranteed increase in WAIT count. D1/H4 structural filters and broker execution
controls are unchanged. A zone touch must still undergo fresh revalidation and
final pre-trade admission; an existing WAIT is never permission to send an order.

### Observation policy v2 (2026-09-09, user-authorized)

This section supersedes the previous pre-watch *account-risk* prerequisite.
WAIT is an observation proposal, never a capital approval.

| Condition | Observation | Execution |
|---|---|---|
| Account weekly PnL/drawdown unavailable | A valid market/event plan may remain WAIT; no account values are synthesized | RiskEngine rejects. Triggered watch stays BLOCKED_DATA for recovery, no order |
| D1 uptrend without bullish EMA, or constructive D1 with bearish H4 | Observe with durable DESK_STRUCTURE_CONFIRMATION | Fresh D1/H4 strict structure check at conversion and final approval; missing bars fail closed |
| Known D1 downtrend, unknown D1 structure, range without bullish EMA | Rejected | Rejected |
| Defensible zone geometry has gross RR 1.45–2.0 | Observation with DESK_RR_CONFIRMATION | Prior 2.0 desk requirement plus current effective-RR gates must pass |
| Invalid geometry, target, stale market facts, event-risk failure | No watch | No order |
| WAIT news has not been read | NOT_CHECKED, fetch real news for observation prerequisites | Fresh news/risk check again before exposure |
| Restart/retry with deferred observation requirements | Requirements persist in the candidate/watch JSON | Fresh confirmation required; direct final_pretrade without confirmation rejects |
| Every deep candidate, including early structure failure | Archive original candles, feature recomputation and same-input policy comparison | Comparison cannot create an OrderIntent |

Evidence and limitations: [observation-policy-audit-2026-09-09.md](observation-policy-audit-2026-09-09.md).
Paper testing only; quote excursions are not broker fills or proven returns.


### Nearest market levels (2026-09-09 audit follow-up)

Production Quant features cluster the available swing population, filter support
below / resistance above latest close, then retain the nearest three on each
side. Previously selecting the highest three first could erase valid nearby
levels in downstream entry geometry. Empty sides remain empty; no synthetic
stop or target is introduced. Regression fixtures cover truncation order,
strict side boundaries, and invalid reference prices.

## ORB strategy replacement

| Condition | Response | Recovery |
| --- | --- | --- |
| SIP unavailable or configured IEX | DATA_BLOCKED; no invented opening volume | Restore authorized SIP access, retry observation |
| Missing/conflicting opening history | No plan for that symbol; visible data reason | A complete, consistent session dataset |
| Quote outside cap, stale or below breakout | No new entry intent or broker submission | Fresh check within the same fixed plan and deadline |
| Quote ages after approval, before initial submission | Mark the provably unsent intent rejected; no broker call | New explicit review through the established admission path |
| Publication transaction fails | Opportunity, admission and consumed-plan link roll back together | Retry transaction; session row serializes ownership |
| Old unclaimed strategy proposal | Withdraw proposal; never reinterpret its prices as ORB | New ORB selection |
| Old UNKNOWN/in-flight entry | Keep durable recovery owner | Reconcile the original broker ID/client ID; never blind resubmit |
| ORB fill without price target | Journal nullable target and original session exit timestamp | Protection and recovery retain the same geometry |
| Session exit due, broker unavailable | Keep stop protection, record failure, retry due exit | Shared durable exit path reconciles existing intent before another order |

| ORB IEX development failure | Required result |
| --- | --- |
| Quote/provider feed differs from saved plan | Refuse entry; never relabel stored bars |
| Feed or strategy revision changes after daily selection | Keep saved evidence; require next-session selection |
| IEX volume below consolidated share floor but dollar liquidity sufficient | Allow observation using IEX RVOL; execution still runs all liquidity/capital gates |
| IEX request returns 403 | Report configured-feed access denial, not a SIP subscription requirement |

## Alpaca-only execution — 2026-09-10

| Condition | Behaviour |
|---|---|
| Non-Alpaca deployment selector | Reject construction; no alternative adapter |
| Old desk calls broker selection PUT | HTTP 410; no state mutation |
| Live URL or lookalike Paper host | Constructor refuses before sending credentials |
| Production mock requested | Refuse; mock is only for explicit local tests |
| Submission timeout / HTTP 5xx | Unresolved, never a definitive rejection |
| IEX quote differs from NBBO | Preserve the admitted limit; no market-order retry |
| No Alpaca observed risk period | Weekly risk/drawdown unknown; entries blocked |
| Risk store unavailable/suspended | Unknown risk; defensive execution remains available |
| Legacy venue journal has unresolved/open state | Refuse cutover; never reconcile it through Alpaca |

## Paper entry allowance rollout (2026-09-10)

`paper-flex-1` widens the maximum entry premium from 0.25 to 1.0 times the
trigger-to-stop distance. This is an experimental execution allowance, not
validated profitability. Risk sizing uses the maximum entry price and the
unchanged stop; the account's monetary risk cap is unchanged.

| Situation | Behaviour |
|---|---|
| Unpublished, unexpired strict ORB plan in Paper | One-time locked revision of max entry, with old/new limit, timestamp, reason and previous parameters retained in evidence |
| Plan already has an opportunity ID | Preserve the full original plan and its limit, regardless of opportunity status |
| Concurrent publication with an older plan | Same session row lock and full-plan comparison refuse stale geometry |
| Quote above the revised maximum / below trigger / stale | Refuse or wait through the existing gates; no market-order fallback |
| Existing position or protective stop | Unchanged |

## ORB automatic approval scheduling

| Situation | Behaviour |
|---|---|
| Newly published ORB proposal | Enqueue immediately after committed publication, without waiting for the remaining symbols |
| Only price outside entry band / spread too wide | Keep proposal as WAIT; next automatic attempt due after 5 seconds, subject to queue availability |
| Missing data, service failure, unknown submission | Existing data/operational backoff or reconciliation applies; never reclassified as a price-only wait |

## ORB all qualifying plans (1.3.0)

| Situation | Behaviour |
|---|---|
| More than 20 instruments qualify | Retain and display every qualifying plan, ranked by relative volume; actionable proposals appear first in the UI |
| Existing capped session | Expand under the session row lock; preserve existing plans, states and publication claims |
| Expansion data request fails | Preserve the existing session; do not replace it with an empty selection |
| Existing 1.1 or 1.2 proposal | Revalidate with its original version and captured parameters |
| Additional plans become eligible | All entry, sizing, exposure and execution gates still apply |


## Experimental early Paper entry (ORB 1.4.0) and skip rearming

| Situation | Behaviour |
|---|---|
| New 1.4 plan | Lower the old high + one-cent trigger by min(25% opening range, 0.05 daily ATR), rounded up to cents; retain original stop and maximum entry |
| Existing unpublished plan | Rebuild from captured bars with the new version; retain old/new geometry and parameters in evidence |
| Already published proposal or position | Preserve geometry and execution state; final admission still uses its original policy |
| User skips | Keep the original proposal SKIPPED; observe the plan for at least 60 seconds and require a fresh quote below the applicable entry level |
| Fresh reset followed by qualifying entry | New opportunity ID and new creation/approval admission; historical skipped proposal stays immutable |
| Executed, approving, discarded or unresolved claim | Never release for another entry |
| Publication using a quote older than the reset | Refuse ORB_STALE_REARM_ADMISSION |
| Missing/stale quote, risk cap, spread, reconciliation, account limit | Existing gates apply; no synthetic data or forced orders |
| Finished or expired plan | Hide from active opportunities; persist history |

Early entry is an experimental Paper hypothesis, not a confirmed breakout or proven improvement.
Results of 1.4.0 are attributed separately from previous versions.


## Auto-buy presentation

| Situation | Behaviour |
|---|---|
| Auto-buy enabled | Hide manual Buy and show authoritative per-proposal state; quantity is read-only |
| Worker queued / processing | Distinguish queued, checking admission, and checking execution; never infer a fill from a valid quote |
| Retry after refusal | Show persisted failure reason, attempts and earliest retry time |
| Durable execution / refusal / skip | Show terminal state even if worker flags lag; no terminal action buttons |
| Mode or execution state changes | Included in desk ETag so the browser cannot keep stale controls via 304 |
| Status projection | Read-only, batched database lookup; never submits or changes a trade |


## Non-long broker quantities during reconciliation

| Situation | Behaviour |
|---|---|
| Broker or ledger reports a negative holding | Preserve signed evidence, record an unexplained-position incident and block the symbol, even if both quantities match |
| Protective sweep sees zero or negative holdings | Report unresolved non-long position; do not create a stop or emergency SELL request |
| Existing protective SELL stops against a short | Treat available long shares as zero for excess-stop cancellation |
| Unexpected short remains | Never auto-convert it to a long or hide the incident; resolution requires verified broker state |
