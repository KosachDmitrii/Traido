# ORB 2.0 — Paper breakout / retest / confirmation

Status: experimental Paper implementation, not statistically validated.
Version: `orb@2.0.0`; previous version parameter hashes and execution evidence
remain unchanged. Authorized by the user's request to develop and implement.
No external research or claimed backtest underlies these initial defaults.

## Entry evidence

Existing instrument, opening-volume, daily-liquidity, ATR, market/sector,
news/earnings, portfolio, spread, reconciliation and Paper checks remain.
The first 09:30–09:35 ET range and previous completed daily data are captured.
Selection itself is only a watch plan. It does not authorize an order.

Replay contiguous completed five-minute bars from 09:35 through evaluation.
Require a bullish close strictly above opening high + $0.01; a later bar
returns within a band of max($0.01, 0.05 daily ATR) around that high; a distinct
later bullish bar closes above the high + $0.01 and above the return-bar close.
A close below the bottom of the band invalidates the pattern. Setup timeout is
60 minutes from breakout completion. No partial/future bar supplies evidence.
Missing completed bars fail closed (even if the vendor is simply late).

At confirmation freeze:
- Entry floor: opening high + $0.01, rounded up to cents.
- Ceiling: min(confirmation close, opening high + band), rounded down.
- Stop: minimum from the retest through confirmation minus
  max($0.01, 0.02 daily ATR), rounded down.
- Target: maximum observed after breakout and BEFORE the retest, rounded down.
  A confirmation that has already touched that target is not actionable.
- Geometry must satisfy stop < floor <= ceiling < target.
- Cost allowance C: max(current ask-bid, ceiling * 10 bps) at admission;
  bar-only observation uses the 10 bps model. This is a conservative modeling
  choice to test, not a measured fee/slippage forecast.
- Require (target - ceiling - C) / (ceiling - stop + C) >= 1.5.
- Fresh bid must be at/above floor and ask at/below ceiling. A long order never
  raises its limit above the frozen ceiling or the sealed approval limit.

Signal deadline is confirmation + 10 minutes, capped by session entry cutoff.
A subsequent completed bar touching stop or target invalidates the signal.
A fresh observed bid touching either also invalidates an unclaimed signal.
After expiry/invalidation only a later complete pattern can authorize entry.
Skip/known unsubmitted discard adds a persistent 60-second barrier before a
new pattern; previous bars cannot be recycled. Filled/in-flight/unknown claims
are not reset. Repeated concurrent publication returns the same opportunity.

## Evidence and execution

The source bars, confirmation/expiry times, target, retest minimum and algorithm
parameters live in immutable `orb_plan.evidence.retest`. Canonical geometry
hashing already includes the complete orb_plan. Approval fetches current bars
without the observer cache and replays the pattern; changed evidence requires
new admission. Observation may cache same-window bars for 30 seconds; missing
facts never grant execution authority.

All existing final execution checks remain. Immediately before submission,
read a fresh quote and check the complete spread, bounds and expiry again.
The original LIMIT is sent; no chase or conversion to a market BUY.
Fill waiting is capped by the remaining signal lifetime, then the existing
cancel-and-reconcile path runs; cancellation is not guaranteed instantaneous.
Filled portions retain the existing protection/reconciliation behavior.

## Exits

Keep a broker-resident protective stop. For this version only:
- Fresh bid at/above the frozen target triggers a close request.
- At/after 30 minutes from recorded opening, bid <= actual average entry
  triggers a close request (not an MFE-based trailing rule).
- Session exit remains one minute before actual exchange close, including
  shortened sessions. It does not require a valid price quote.

Target/time exits are server-monitored, require a working service and broker,
and execute through the existing close owner. It cancels/verifies protection,
checks signed broker holdings and uses durable intents. No second resting
profit SELL competes with the protective stop. Market execution can differ
from trigger price. Broker errors retry; UNKNOWN is never treated as canceled.
No new target or stop is retroactively attached to older positions.

## Review and validation

UI shows signal states, entry zone, target, stop and expiry; provisional prices
are hidden until confirmation. Positions project the immutable target without
changing ledger protection fields. Journal entry evidence keeps the complete
orb_plan; exit reasons distinguish target, no-progress and session exit.
Current-version Paper results stay separate from legacy results.

Regression scenarios cover distinct completed bars, boundaries, no lookahead,
invalid/missing source history, inadequate reward, expiry, stop/target touch,
replay after serialization, skip/replacement CAS, real admission/execution
with a mock broker, monitored exits and legacy behavior. These demonstrate
software behavior; they do not establish positive expectancy. Before further
loosening or Live use, evaluate execution costs, drawdown, out-of-sample results,
and fill-level account reconciliation on sufficient independent sessions.
