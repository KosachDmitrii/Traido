# ORB replacement decision — 2026-09-10

The active strategy is `orb@1.1.0` (IEX development support). It replaces the desk's multi-timeframe pullback,
arrival-quality score, aggressiveness slider and mandatory price-target/R:R admission.
Historical executions, their evidence and protective orders are not rewritten.
Unclaimed old proposals are withdrawn; an in-flight or UNKNOWN order retains its
existing recovery owner. Market data is Alpaca only; execution remains with the
selected Paper broker, Alpaca or IBKR. Live entry remains prohibited.

## Research and application choices

Reference: Zarattini, Barbon and Aziz, *A Profitable Day Trading Strategy for the
U.S. Equity Market*,
https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf.
The paper studies both directions. Its reported results are not results for this
application or evidence that its long-only implementation will be profitable.

The first five-minute interval is 09:30–09:35 America/New_York. Only completed
bars are used. Opening price must exceed $5, prior 14-session mean volume must be
at least 1 million shares, and ATR must exceed $0.50. ATR here is the arithmetic
mean of 14 true ranges, requiring 15 prior daily closes. Relative volume is the
first five-minute volume divided by the mean volume of that *same interval* in
14 prior sessions. It must be at least 1. The entire eligible listed universe is
measured before selecting the top 20; there is no quant top-50/deep top-40 or AI
score stage. Selection among candidates with complete valid data is explicit;
missing histories are separately counted, not described as failed setups.

Application adaptations, not universal trading standards:

- Long-only; the first five-minute candle must close above its open.
- Trigger is the opening high plus one equity tick. A current bid at or above
  that trigger is required, so a widening ask alone cannot create a signal.
- Stop is 0.10 daily ATR below the trigger, rounded down to an equity tick.
- The disclosed maximum buy price is trigger plus 0.25 of planned risk per share,
  rounded down. The order is a marketable limit at that cap; sizing uses this
  worst permitted fill, and actual fills can be better. A manual confirmation
  system does not reproduce the paper's exact automatic breakout fills.
- There is no price target. Exit is the protective stop or scheduled liquidation
  one minute before the actual exchange close, including early-close sessions.
  Entries stop five minutes before that exit deadline. The exit loop polls every
  five seconds. Broker outage or a process outage can delay liquidation; the
  broker stop remains the protective mechanism and overdue exits are retried.
- A maximum five-second quote age is checked again immediately before a new
  entry submission. UNKNOWN recovery looks for an existing order rather than
  requiring a new signal or submitting another order.
- Existing portfolio, broker identity, Paper, liquidity/spread, event-risk,
  sector concentration, macro/sector and reconciliation controls remain in force.
  These additional gates and the application's risk sizing also differ from the
  paper. They must be visible separately from ORB observation.

The data feed is explicit. Paper defaults to IEX, as required by the user;
SIP is only used when explicitly configured. The profile below supersedes the
initial SIP-only implementation. No subscription is required for IEX.

For IEX, the 1-million-share consolidated-volume condition is not applied.
Selection instead requires at least $20 million of mean observed daily dollar
volume over the 14-session baseline, matching the existing execution liquidity
floor in units (execution independently checks its 20-bar history). Opening
relative volume compares IEX with IEX for the same 5-minute window; no market-share
multiplier invents consolidated volume. The existing execution liquidity,
participation, spread, quote-age, and portfolio risk gates are unchanged. IEX
prices and volumes describe one exchange, not NBBO or total US market turnover.
This is a Paper development adaptation, not a reproduction of published SIP
backtest results. SIP retains the original share-volume selection condition.

Each plan records its feed. Quotes carry adapter feed metadata; a mismatched
quote or provider is rejected. A saved session is never reinterpreted after a
feed/version change; a new selection requires the next session. Prior versions
remain readable for audit and existing-position recovery. The registry gets a
new immutable version rather than rewriting the existing parameter hash.
Access probes operate on the configured feed and IEX errors never request SIP
subscription. There is no silent fallback or artificial quote/volume scaling.

## Persistence and execution

`orb_sessions` stores the original bars, calculated inputs, session selection and
immutable price/time geometry. A restart reuses that session's selection.
Observation may change a state or quote but cannot erase a consumed plan's
opportunity identifier. Publication locks the session row and writes the
opportunity, its creation admission and the consumed-plan link in one transaction.
Thus skip, fill, restart and stop-out cannot re-create the same plan's opportunity.

Approval uses the existing durable request/CAS/OrderIntent/ApprovalEvidence path.
It verifies membership and exact geometry in the saved selection, rereads today's
opening interval, recomputes the plan from the saved prior sessions, checks a
fresh bid/ask against the price cap, sizes risk, and seals the evidence. Price
caps do not imply guaranteed fills, and stop prices do not guarantee maximum
realized loss through gaps or slippage.

The journal and recovery explicitly support `target = null` with a timestamp
exit. Unlinked older positions do not receive a fabricated ORB plan or a new
scheduled liquidation time. Their protective reconciliation and manual close
remain available.

## Verification contract

New tests cover exact volume/ATR inputs, missing and contradictory bars, partial
opening intervals, IEX rejection, quote age, price bounds, early close, full-pool
ranking, restart persistence and atomic publication. Execution lifecycle tests
use synthetic ORB bars through the actual admission functions, keeping the real
sizing, durable intent, partial-fill, protective-stop and recovery assertions.
Tests of removed conviction ranking, top-40 rotation and score-based admission
are replaced by the ORB contracts, not used as evidence of profitability.

Production acceptance additionally requires successful migrations, health,
confirmed access to the configured feed, valid opening data and a complete observed session. A
green unit suite alone does not establish those facts or a profitable strategy.
