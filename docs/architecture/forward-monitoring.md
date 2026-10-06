# Continuous forward observation (2026-10-06)

The owner authorized independent observation without changes to existing trading
logic. The monitoring layer has no broker/market-data port and calls no execution,
reconciliation, promotion, notification, or strategy evaluation functions. It
reads saved evidence plus existing in-process scanner/reconciliation status. Only
`monitoring_samples` and `monitoring_reports` may be written. No trading policy,
quantity, exit, feed or mode is changed.

A lifespan-owned observer samples every 60 seconds and regenerates a persisted
report every 300 seconds. Closing the browser does not stop it. GET
`/api/v1/diagnostics/forward-monitoring` reads the saved report, with observer
health and a stale flag; it does not initiate work. The Evaluation page polls
this endpoint every 60 seconds. Restart cannot create two samples for one UTC
minute. Failure records the exception class without sensitive exception text,
then retries; cancellation drains an in-flight database tick before shutdown.
Postgres observer transactions have local 5-second statement and 1-second lock
timeouts; these never change execution's connections/settings.

Reports retain one snapshot per exchange date and show the last 30 completed
NYSE sessions (holidays, early closes and ET date handled by the existing
calendar). Missing sessions are visible, never fabricated as healthy observations
or zero-activity days. Coverage requires samples across RTH with no gap over
180 seconds. Premarket startup does not make a completed trading session fail.
Scanner and reconciliation health, unresolved states and duplicate broker IDs
are measurements, not new trading gates. "Passed" technical criteria means only
the specified observed checks passed; it does not prove every execution scenario.

A hash of entry/ORB/risk source code, active versions, resolved entry thresholds,
risk-engine defaults, parameters, exit policy, broker environment and feed defines
a contiguous forward cohort. Changes restart cohort counting. Versions are
reported separately. Backtests, legacy strategy trades, unknown P&L, missing
entry timestamps and entries before the observed cohort are excluded from
cohort performance. Historical daily journal results include all Paper strategy versions; a day without
trades and complete observation has no asserted zero P&L. They are a separate view and are not
proof of the new cohort. Funnel counters include repeat evaluations, not unique
signals. Proposal/intent status counts count durable rows instead.

Each recorded entry is checked against its **saved** risk decision: whole positive
shares, positive prices/equity, cash, maximum position notional and approved risk
size. Missing/non-finite evidence is "insufficient_data". This does not certify
aggregate concurrent pending commitments or implement the discussed 15/20/20
allocation scheme. Current sizing remains unchanged.

Account net-liquidation/day change/drawdown are read from an already initialized,
unsuspended Paper risk period, with a source age of at most 180 seconds. No period
is automatically started. Multiple periods are ambiguous and return no account
measurement. These are sampled account values, including open exposure, not
attributed strategy net returns or cash-flow-adjusted investment returns.

30 full sessions and 100 closed trades per version are explicit checkpoints, not
statistical proof. Gross journal expectancy and profit factor are shown; net
profitability is **not asserted** without complete fees/subscriptions, historical
open-position valuations, external cash-flow verification and held-out validation.
The result remains insufficient where evidence is missing; Live readiness is
always false. Existing promotion thresholds and gates are untouched. This layer
is continuous forward monitoring, not a newly implemented historical ORB backtest.

Regression coverage checks operational-table immutability and no broker factory
calls, saved budget boundaries for small accounts, unavailable/non-finite data,
separate versions/cohorts, exchange calendar, missing intervals, migration
upgrade/downgrade, tick retry, and draining shutdown.
