# Continuous Paper discovery — owner decision, 2026-10-01

The owner requested a review of every entry stage and continuous discovery,
after two weeks without a reported purchase. This decision extends an existing
Paper strategy; it does not authorize Live, increase risk limits or bypass gates.

## Search timeliness — owner decision, 2026-10-07

An Oct 6 audit found a roughly one-hour gap between completed discovery passes
while M5 loading continued. The full observation pass was awaited before rolling
discovery. The owner authorized repairing search latency and measuring freshness.

- A scanner-owned, single-process discovery loop reads independently every 30
  seconds during Paper RTH while scanning is enabled and the kill switch is off.
  The ordinary scanner also refreshes before awaiting full observation. Both
  paths share one discovery lock and the existing Alpaca account quota.
- Each pass handles one window: the newest completed M5 window first, then the
  oldest pending window. Successful ends are committed atomically with added
  plans in the existing session JSON. Failure does not advance completion;
  restart reloads progress. Catch-up ends at the actual session close, including
  early closes. Previously claimed symbols and geometry are never replaced.
- Catch-up creates a WAIT plan, never a historical permission to buy. Current
  bar replay, quote freshness, expiry and every existing admission gate remain.
- M5 reads have a 60-second deadline per 100-symbol batch; no whole-universe
  short deadline rejects healthy paced scans. Two historical readers remain;
  failure/cancellation drains sibling reads before releasing the lock.
- Full observation has its own configured scanner-cycle timeout, so shielding
  callers cannot leave an unbounded shared task. Scanner health degrades after
  progress is older than max(300 seconds, twice the configured scan interval).
  Discovery health separately reports a stopped task or 300-second heartbeat gap.
- Logs expose observation stage duration, discovery duration, window lag and
  backlog; quote logs expose source/receipt/evaluation timestamps and age.
  Metrics measure quote request latency and account-quota wait separately.

These bounds diagnose and recover operational delays; they do not prove a
five-second quote age can always be met or that a trade opportunity was lost.

### Bounded observation — owner decision, 2026-10-07

The owner authorized professional operational repairs after the afternoon audit
found discovery continuing while the full scanner heartbeat exceeded 600 seconds.
This changes processing and diagnostics, not strategy or capital policy.

- Each full observation loads/replays at most 100 plans, oldest persisted
  `last_checked_at` first (falling back to existing `observed_at`). Checking a
  claimed/open symbol advances scheduling without renewing its market evidence.
  Subsequent portions include the remaining universe;
  there is no permanent top-N exclusion. While recent checks remain pending,
  the scanner retries after 5 seconds, subject to the shared Alpaca cooldown.
- At most eight actionable candidates enter admission per portion, with two
  evaluators per full/priority path. A shared symbol reservation prevents the
  two paths from evaluating the same symbol simultaneously. Each evaluation
  has a 45-second deadline; failure is DATA_BLOCKED, never an admission.
  The existing overall cycle deadline remains authoritative.
- Priority stream observation handles at most 100 symbols and eight candidates;
  deferred events remain queued. A failed/cancelled candidate history request
  returns its events to the queue and preserves newer received corrections.
- Market-bar writes deduplicate identical provenance keys and upsert 100 rows
  per SQL statement in one transaction. Last received correction wins; feed,
  symbol, timeframe, UTC timestamp and validation retain their meaning.
- Busy websocket processing checks session membership every 30 seconds and
  incrementally subscribes newly discovered names to bars/updatedBars. The
  quiet timeout reconnect path and subscription acknowledgement checks remain.
- Older observation timestamps cannot overwrite a newer saved projection.
  Claims and machine transitions retain their existing authority.
- Desk diagnostics separately show checked-in-five-minutes, pending checks
  and DATA_BLOCKED reason totals. A recent check is not a fresh market quote.
  Logs identify history/snapshots/replay/entries/completed and duration;
  public metrics report observation totals, pending, recent checks and blocked
  states. Funnel completion counts describe the checked portion rather than
  claiming the entire universe was evaluated in one cycle.

Regression coverage includes SQL batching/corrections/provenance, whole-universe
rotation, stale projection rejection, isolated symbol failures, concurrent
symbol ownership, busy-stream subscription growth and priority recovery.
No synthetic candle, stale-quote acceptance, historical entry permission,
Live order or change to manual_target exits is authorized by this repair.

## Confirmed problems

`runtime.discover` returns the persisted `all_qualified` selection all day.
Opening relative volume below 1 or a non-bullish 09:30 bar permanently excludes
a name, regardless of later movement. The quote/bar observer is running, but
it only observes selected names. A constant selection count is consequently
expected and is not evidence that prices stopped updating.

On October 1, GMAB reached BUY_ALLOWED at 13:57:47 and 13:57:53 UTC before the
sector resolver repair deployed. The observed stop was final admission due to
unavailable sector classification. This disproves an explanation of every
missing purchase as absence of entry signals. Available logs are not a complete
two-week decision dataset; no conclusion about all missed trades is made.

The final-admission and portfolio-risk refusals previously lived mainly in the
board/current projection. Subsequent observations could replace the visible
reason. They now log the exact stage and preserve the most recent refusal,
with its time, for symbol inspection. Historical missing evidence is not invented.

## Implemented policy

- Preserve opening ORB `orb@2.2.0` and every existing plan/claim.
- Add `orb@2.3.0` rolling M5 discovery during RTH, Paper only.
- Inspect unselected instruments passing existing daily liquidity/history/ATR
  requirements after each completed M5 boundary. The daily source evidence and
  instrument provenance persist for restart recovery.
- A new range must be bullish, priced above $5 and have volume at least the
  average of **the same clock window in 14 completed prior sessions**. Missing
  bars are a refusal, not a zero-volume substitute. No cumulative daily-volume
  approximation is used.
- A selected range remains immutable. Selection is WAIT, not BUY_ALLOWED.
  Require a subsequent breakout, distinct retest and distinct confirmation.
  Preserve observed targets, previous-day-high cap, 1.5 effective R:R, costs,
  freshness, spread, market/sector/event gates and portfolio limits.
- Re-read the exact range and replay its subsequent bars at final approval.
  Version support extends through schema, admission, publication, viability,
  approval, broker submission, expiry, target exits and UI.
- Atomically append only previously absent symbols under the same session lock
  used by publication. A delayed discovery never rewrites a claimed plan.
- Discovery vendor failures retain existing plans/positions and report
  DATA_BLOCKED; retry after 30 seconds. Successful windows are not refetched on
  each scanner tick. Discovery does not replace the independent watch/exit loops.
- Show latest discovery time, number checked/added and refusal reasons. Journal
  statistics for 2.3 are separate from 2.2 and exclude backtests.

## Entry and exit audit

| Stage | Actual authority | Finding / evidence |
| --- | --- | --- |
| Universe | Alpaca reference data + deterministic eligibility | BROAD, no top-N cap; SIP only |
| Daily filter | 15 completed sessions for ATR14; mean 14-day volume | $0.50 ATR and 1M share minimum remain; not an agent confidence score |
| Range selection | Deterministic form_plan | Fixed opening selection was the coverage restriction; rolling windows added |
| Setup / entry | Completed M5 replay and fresh bid/ask | Breakout → retest → confirmation; no buying merely because a company is strong |
| Market | FRED market agent and market gate | Missing/stale data blocks; prior monthly UNRATE freshness defect already repaired |
| Sector | Verified classification + benchmark bars | GMAB classification repair already deployed; unknown industry remains blocked |
| Final admission | Range reread, replay, sealed geometry/quote | Refusals now recorded by stage; no gate bypass |
| Risk | Deterministic RiskEngine and fresh portfolio/context | Existing size, loss, concentration and event controls remain |
| Authorization | User confirmation or configured automatic trigger | Discovery does not enable automatic approval; existing setting retained |
| Execution | ExecutionService → durable intent → Alpaca Paper | Revalidation, idempotency, READY and reconciliation gates remain |
| Position / exits | Background position assessment + ExecutionService | Existing owner `manual_target` policy means target/operator sell only; reference stop is not a loss ceiling |
| Reconciliation | Broker truth and persisted intents/ledger | Unknown orders/positions remain blocking; no fabricated fill or forced test trade |

The current active ORB path is deterministic. Legacy AI thesis/scoring agents
are not invoked on every observed name; their idle state does not establish an
entry execution failure. The FRED context agent runs only after an entry trigger.

## Validation and remaining evidence

### 2026-10-08 scanner cadence correction

Production full observation stopped completing after 14:19 UTC while SIP bars
and independent discovery continued. Readiness reported no scanner progress
for more than 600 seconds. Bounded pending portions run before the next normal
slot; the scheduler incorrectly advanced one whole interval for each portion.
Once pending cleared, normal waiting used the accumulated future due time.
The schedule now advances only past slots whose time has actually passed at
completion. Early portions and operator wakes retain the next future slot.
Deterministic regression tests reproduce the old failure and cover repeated
portions, early wakes and a portion crossing a slot. Existing cadence/overrun
tests continue to apply. This changes scheduling only, not strategy admission,
size, order authorization, broker execution or the owner Paper exit policy.

After deployment, the scanner resumed, but 100-plan portions completed roughly
every 30 seconds against over 1,300 plans. The full sweep portion is now 200 to
amortize history/context work; the SIP priority portion remains 100, and heavy
candidate evaluation remains bounded to 8 with the same admission gates. The
real observation-path coverage test checks 1,401 plans across bounded portions,
including the tail, without sending orders. Check age still uses the original
five-minute window; no stale fact is made fresh by increasing that window.

The first deployed rolling pass inspected 1,378 unselected names and added 146
plans (including MSFT, ABNB and AXP). It also exposed a pre-existing eligibility
gap: reference names explicitly describing geared/inverse objectives were not
excluded. Long shares bought with cash can still be shares of a leveraged fund.
An additional central reference-name check now rejects explicit numeric daily
multipliers, inverse/leveraged labels and documented ProShares geared families
at universe eligibility and again at final admission for already saved plans.
Generic short-duration bond names are not treated as inverse funds. This is a
rejection rule, not certification of unnamed funds as unleveraged.

Sources: [ProShares geared ETF directory](https://www.proshares.com/our-etfs/find-leveraged-and-inverse-etfs),
[Direxion TMF/TMV](https://www.direxion.com/product/daily-20-year-treasury-bull-bear-3x-etfs),
[Direxion TNA/TZA](https://www.direxion.com/product/daily-small-cap-bull-bear-3x-etfs).
Existing positions/claims remain intact; the check refuses new entry admission.
Two historical-window readers now overlap network latency while sharing the
adapter's existing account quota. No request-limit bypass is introduced.

Regression scenarios include valid rolling entries, same-clock volume,
incomplete/wrong-clock history, red/low-volume/future ranges, stale quotes,
altered confirmation, order-free negative admission, preserved existing claims,
restart reuse and discovery failure/retry. Existing execution tests exercise
both strategy versions and both Paper exit policies against a mock broker;
target/time/stale-quote exit tests also cover the new version.

These tests prove code-path behavior, not trading profitability. Both policies
are experimental Paper strategies. A real SIP historical replay with fills,
costs and out-of-sample results has not been completed. Increasing trade risk
or loosening R:R/quote/event controls is not supported by that missing evidence.
Alpaca Paper fills are simulated; passing tests does not establish real fills.

The owner manual-target exit override remains in effect. Automatic loss stops
and timed exits must not be claimed active under that override. Choosing a
protected exit policy is a distinct owner decision, not a hidden discovery change.
