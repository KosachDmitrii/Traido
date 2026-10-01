# Continuous Paper discovery — owner decision, 2026-10-01

The owner requested a review of every entry stage and continuous discovery,
after two weeks without a reported purchase. This decision extends an existing
Paper strategy; it does not authorize Live, increase risk limits or bypass gates.

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
