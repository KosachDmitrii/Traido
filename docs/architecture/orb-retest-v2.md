# ORB 2.1 — Paper breakout / retest / confirmation

Status: experimental Paper implementation, not statistically validated.
Version: `orb@2.1.0`; previous version parameter hashes and execution evidence
remain unchanged. Authorized by the user's request to develop and implement.
This remains the same ORB strategy, with a versioned context/measurement revision;
it is not a second trading strategy.

## Entry evidence

Existing instrument, opening-volume, daily-liquidity, ATR, market/sector,
news/earnings, portfolio, spread, reconciliation and Paper checks remain.
The first 09:30–09:35 ET range and previous completed daily data are captured.
Selection itself is only a watch plan. It does not authorize an order.

Replay contiguous completed five-minute bars from 09:35 through evaluation.
On process start or reconnect, recover that complete interval from Alpaca SIP
in full-session batches before publishing an observed phase. A newly streamed
bar cannot preserve `WAIT_BREAKOUT` while an earlier interval is missing;
incomplete recovery is `DATA_BLOCKED`, never a fresh-looking phase 1 state.
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
- Previous completed session high (PDH) is structural context. When it is above
  the entry floor, below the observed impulse target, and the confirmation has
  not closed above it, PDH caps the target. The normal geometry and effective-RR
  gate then reject the setup if that reachable path is too small. A completed
  confirmation close above PDH leaves the observed target intact. PDH at/below
  entry is recorded but does not restrict the target.
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

The source bars, confirmation/expiry times, raw and PDH-adjusted target, retest
minimum, PDH state and algorithm parameters live in immutable
`orb_plan.evidence.retest`. The same record captures confirmation candle
body/range, close location, upper-wick/range and volume relative to all earlier
completed five-minute bars available in the replay. These measurements are
explainability and later-ranking evidence, not newly invented entry thresholds.
Canonical geometry
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

The deployed Paper owner policy `TRAIDO_PAPER_EXIT_POLICY=manual_target`
overrides automatic stop installation, no-progress and session exits. A Paper
position exits only through an explicit operator sell or its immutable stored
card target. Positions may carry overnight. Entry still stores the structural
reference stop and risk sizing still uses entry-to-stop distance, but that stop
is not a broker-resident maximum-loss guarantee under this owner policy.

Stored-target exits are server-monitored, require a working service and broker,
and execute through the existing close owner. It cancels/verifies any legacy
protection, checks signed broker holdings and uses durable intents. Market
execution can differ from trigger price. Broker errors retry; UNKNOWN is never
treated as canceled. No new target or stop is retroactively attached to older
positions. Live remains prohibited.

## Review and validation

UI shows signal states, entry zone, target, stop and expiry; provisional prices
are hidden until confirmation. Positions project the immutable target without
changing ledger protection fields. Journal entry evidence keeps the complete
orb_plan; exit reasons distinguish target, no-progress and session exit.
Current-version Paper results stay separate from legacy results.

Regression scenarios cover distinct completed bars, boundaries, no lookahead,
PDH target capping/clearance, confirmation measurements, invalid/missing source
history, inadequate reward, expiry, stop/target touch,
replay after serialization, skip/replacement CAS, real admission/execution
with a mock broker, monitored exits and legacy behavior. These demonstrate
software behavior; they do not establish positive expectancy. Before further
loosening or Live use, evaluate execution costs, drawdown, out-of-sample results,
and fill-level account reconciliation on sufficient independent sessions.
