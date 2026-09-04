# Traido — Production Architecture v1.0

**Status: conceptually frozen.** Changes below this line require an explicit
decision record, not a refactor. Implementation proceeds by stages; see
`docs/architecture/staged-plan.md`.

Traido is an **autonomous AI-assisted systematic trading platform with
deterministic risk management and broker execution.**

It is not a "Claude trading bot" and not a charting app. The distinction is
architectural, not marketing: an LLM is one input among many, and it sits
*upstream* of every control that can move money.

---

## 0. Prime directive

```
AI thinks.
Quant calculates.
Strategy proposes.
Risk decides if trading is allowed.
Human or Autopilot authorizes.
Broker executes.
Position Manager protects.
Reconciliation verifies.
Review learns.
```

**No LLM can bypass Risk or Execution controls.**

The broker is an execution venue, not a brain. IBKR positions itself as
self-directed brokerage and makes no investment decisions for the account
holder; responsibility for every order stays with the owner. Traido is
therefore designed as a **fault-tolerant trading system**, not an AI script —
the broker guarantees neither absence of outages, nor best price, nor that any
given order executes at all.

---

## 1. Layer map

```
Market Data → Quant → AI Agents → Strategy → Liquidity → Risk
    → Approval | Autopilot → Execution → Broker
    → Reconciliation → Position Management → Review
```

| Layer | Responsibility | May place orders? |
|-------|----------------|-------------------|
| Market Data | OHLCV, quotes, trades, corporate actions | no |
| Quant Core | All objective computation | no |
| AI Agents | Interpretation and judgement | no |
| Strategy Engine | Proposes a `TradeCandidate` | no |
| Liquidity Engine | Executability and expected slippage | no |
| **Risk Engine** | **Deterministic final veto** | no |
| Approval / Autopilot | Authorization | no |
| **Execution Service** | **Sole code path to the broker** | **yes** |
| Position Manager | Stops, targets, exits | via Execution |
| Reconciliation | Truth sync against the broker | cancel only |
| Review | Learning, never live control | no |

---

## 2. Non-negotiable invariants

These are enforced by tests in `tests/unit/test_capital_safety.py`, which runs
as its own CI job. If a change breaks one, the change is wrong.

1. **Risk is deterministic code.** No LLM output reaches `RiskEngine` limits,
   SQL, or the broker.
2. **One execution path.** `place_order` is reachable only from
   `ExecutionService`. Enforced by a source-scanning test.
3. **Long-only, unleveraged, US equities and ETFs in V1.**
4. **Kill switch is fail-closed and LLM-independent.** Durable across restart;
   ambiguity reads as "halted", never as "trading is fine".
5. **Rejecting a trade is always preferable to ambiguous automation.**
6. **Paper and live share one architecture.** Selected by configuration.
   No `if live:` branches in strategy, risk, or execution logic.
7. **Every position gets a protective exit, or it does not exist.** A fill
   without a working stop is an incident, not a state. Any actual filled
   quantity is either protected for exactly that quantity or emergency-closed.
   A resting stop is *evidence* of protection, re-verified every reconciliation
   pass, not a guarantee: it is the venue's order, and whether it fires is the
   venue's behaviour. Failing to read it back is "unknown", never "fine".
8. **Broker truth is authoritative for execution state.** Local records are a
   claim about the world; the broker is the world. Where they disagree, the
   broker wins or the state becomes `UNKNOWN`.
9. **Every broker mutation has a durable intent.** Entries, discretionary
   exits, and emergency closes are all written as an `OrderIntent` before
   transmission, because recovery cannot ask about an order it has no record
   of. Purpose is recorded explicitly (`IntentPurpose`), never inferred from
   BUY/SELL.
10. **One idempotency key, at most one broker order.** Enforced by a unique
    index, not by application logic alone. This holds for exits and emergency
    closes exactly as it does for entries.
11. **`UNKNOWN` blocks conflicting trading.** No new entry, no conflicting
    order, no autopilot action for that symbol until reconciliation resolves it.
    `UNKNOWN` is never quietly downgraded to `CANCELED`, `FAILED`, or "awaiting".
    An unresolved emergency close blocks a discretionary exit on that position.
12. **A partial fill is a position, not a failure.** It follows the ordinary
    lifecycle at the actual filled quantity. A partial *exit* reduces the local
    position by the filled quantity only and leaves the remainder open; the
    ledger never closes a position twice.
13. **Protection quantity equals the remaining position quantity.** A stop
    larger than the position would sell shares we do not own; a missing stop
    leaves the remainder naked. Both are repaired or the remainder is
    emergency-closed.
14. **One open position per symbol.** Brokers report a single net position per
    symbol, so a second local row could never be reconciled against it — and
    each row would carry its own stop for shares the other also claims. A
    second entry is refused before transmission, and the ledger refuses to
    record one regardless of how it is reached.
15. **New V1 entries are RTH-only.** This restricts *entries*, not all broker
    interaction: protective exits and reconciliation run whenever needed.
16. **A missing live quote is not a passed spread check.** Without fresh
    top-of-book the liquidity gate fails closed (`LIVE_QUOTE_REQUIRED` /
    `QUOTE_STALE`). Modeled spreads are labelled `modeled` and cannot satisfy a
    live gate. A service built with no market-data port at all is the same
    claim one level up — it refuses (`MARKET_DATA_NOT_CONFIGURED`) rather than
    skipping the gate, and the routes cannot build one, because that omission
    once disarmed every liquidity check on the desk without saying so.
17. **New exposure requires a `READY` broker link.** Entries and discretionary
    exits are refused in any other connection state. Reconciliation continues,
    and existing protective orders are left in place rather than cancelled —
    but "left in place" is not "confirmed working", so they stay external state
    that each pass has to read back.
18. **Market-data vendor and execution broker are independent choices.**
    Neither may be inferred from the other.
19. **Broker instrument identity is explicit.** For IBKR that means a resolved
    `conId`. An ambiguous ticker is a rejection, never a routing decision left
    to the broker.

---

## 3. Quant Core — the mathematical brain

Claude does not compute RSI. Python computes everything objective; the agent
receives a finished snapshot:

```json
{ "trend": "bullish", "rsi": 47.4, "relativeVolume": 1.83,
  "bullishEngulfing": true, "distanceToSupportPct": 0.8 }
```

Scope: candlesticks, indicators, market structure (HH/HL, LH/LL, support,
resistance, breakout, retest, gap), volume, chart patterns, momentum,
volatility, regime, statistics, backtesting.

**Multi-timeframe is mandatory:** 1D global trend → 4H structure → 1H setup →
15m entry. 5m only where a strategy explicitly requires it.

**Decimal, not float, on the money path.** Position sizing, prices and P&L use
`Decimal`. Float drift is acceptable in research, not in an order.

---

## 4. AI Agents

Eight agents: Supervisor, Market, Technical, News, Fundamental, Strategy,
Position, Review.

**An agent is defined by its tool allowlist, not by its prompt.** Strategy
Agent may call `getTechnicalAnalysis`, `getQuantStats`, `getMarketRegime`,
`getNewsContext`, `getFundamentals`, `getPortfolioRisk`, `createTradeCandidate`.
It may never call `placeOrder`, `changeRiskLimits`, raw SQL, or read secrets.

The allowlist is a **runtime mechanism**, not a documentation convention.

Contract details: `docs/architecture/agents-and-tools.md`.

---

## 5. Risk Engine — above every agent

The safety envelope, not a profitability strategy. V1 hard policy:

```
US stocks / ETF only          Price >= $10
OTC, penny stocks             BLOCK
Options, futures, forex, crypto   BLOCK
Short selling, leverage       BLOCK
Illiquid names, wide spreads  BLOCK
New entries outside RTH       BLOCK
```

Plus per-trade risk, position size, daily/weekly loss, portfolio drawdown,
sector exposure, correlated exposure, open position count, minimum liquidity.

Limits live in `configs/v1_paper.json` and are loaded as data. No agent, and no
LLM, can widen them at runtime.

---

## 6. Liquidity Engine — a separate gate

Not "AAPL looks good", but: average dollar volume, bid/ask spread, order book
quality, current volatility, expected slippage, position size versus volume.

```
Strategy BUY → Liquidity Check → Risk Check → Execution
```

This exists because brokers warn explicitly that market orders can fill at
undesirable prices and stops can trigger far from the stop price. Edge that is
smaller than modelled cost is not edge.

---

## 7. Order lifecycle

`placeOrder() → done` is not a model of reality.

```
CREATED → SUBMITTING → SUBMITTED → ACKNOWLEDGED
        → PARTIALLY_FILLED → FILLED
also: REJECTED · CANCEL_PENDING · CANCELLED · EXPIRED · UNKNOWN
```

`UNKNOWN` is a first-class state. A broker that does not answer is not a broker
that did nothing.

**Idempotency.** Every order intent is persisted *before* transmission and
carries a stable `client_order_id` plus an `idempotency_key`. If the connection
drops after sending and before the response, recovery **resolves the existing
intent** — it never re-sends a fresh buy.

**Reconciliation** runs continuously and compares Traido state against actual
broker state. Divergence is an alert, and unprotected exposure is remediated
before anything else.

---

## 8. Protective exit

Every entry decides its stop, target and exit policy at open. A stop is not a
guarantee: it can fill far from the trigger, and a stop-limit may not fill at
all.

Position size is therefore constrained *before* entry against: normal stop, gap
scenario, high-volatility scenario, liquidity failure, and stop-limit non-fill.

---

## 9. Modes

**Confirmation (default).** Every entry and every exit is authorized by the
operator via Telegram or the desk.

**Emergency actions do not wait for a human.** Hard stop, portfolio kill
switch and broker risk events act automatically. The phone may be off.

**Autopilot exists from day one and ships DISABLED.** It is enabled per
approved strategy version, never globally, and never because an agent found a
new pattern.

**Fail-safe.** `Normal → Degraded → Fail Safe`. In Fail Safe: no new positions;
protective broker orders stay live; reconciliation continues; alerts fire;
manual broker access remains available.

---

## 10. Strategy promotion gate

Agents may propose. They may not promote.

```
Proposal → Backtest → Out-of-sample → Walk-forward → Paper
        → Human approval → Production
```

Live capital follows results, never a calendar:

```
Backtest → Out-of-sample → Walk-forward → Paper forward
        → Execution validation → Risk validation
        → Small live → Confirmation live → Limited Autopilot
```

Backtests must model commission, spread, slippage, partial fills, latency,
corporate actions, delistings, survivorship bias and look-ahead bias, and must
report against a benchmark. **Beating nothing is not a result:** +8% while SPY
returns +18% at equal or worse risk is a failure.

---

## 11. Provider independence

Market data, news, fundamentals, macro and the broker sit behind ports. Traido
is never coupled to one vendor.

This is also a licensing boundary. Non-professional market data agreements are
scoped to personal use and restrict redistribution, with additional conditions
on non-display and derived use. A personal desk is simple; a product is not.
The market-data layer therefore carries explicit metadata — `Provider`,
`LicenseType`, `UsagePolicy`, `RedistributionAllowed`, `NonDisplayAllowed`,
`CommercialAllowed` — so the question is answerable before it becomes urgent.

Vendor selections: `docs/architecture/vendor-lock.md`.

---

## 12. Memory, audit, observability

**Memory** is three distinct things: market memory (regime history), strategy
memory (setup outcomes), trade memory (every real trade with entry, exit,
technical/quant/news/market/risk state at decision time, MFE, MAE, P&L, fees,
slippage).

**Audit** records every step from candidate to fill, and is append-only.

**AI observability** records per agent run: model, prompt version, input,
output, tool calls, tokens, latency, cost, confidence, errors. An agent whose
cost and accuracy are unmeasured cannot be evaluated or retired.

**Broker health monitor** checks connectivity, authentication, account sync,
market state, order/position sync and latency. `BROKER DEGRADED → new entries
disabled`.

---

## 13. Security

Credentials never live in code, prompts, git, or logs. The broker agreement
makes the account holder responsible for credential security and for anything
done with those credentials.

Secrets manager, encrypted tokens, least privilege, 2FA where available, audit,
network controls.

---

## 14. Implementation status

Conceptual freeze does not mean built. Verified against the tree, not assumed:

| Layer | Status |
|-------|--------|
| Market data (OHLCV, Alpaca + fixtures) | built |
| Quant: indicators, candlesticks, momentum, volatility, regime, correlation | built |
| Quant: chart patterns | partial — double top/bottom only |
| Quant: breakout / retest / gap detectors | missing |
| Multi-timeframe confluence | partial — D1 + H1 live, config declares four |
| Agents: supervisor, technical, strategy, position, review, scanner | built (deterministic) |
| Agents: market, news | partial — degrade to neutral stubs without keys |
| Agent: fundamental | missing |
| Agent tool allowlist enforcement | missing — documented only |
| Strategy registry / versioned promotion | built — `strategy_versions` + promotion gate (Stage 8) |
| Risk Engine + sizing + limits + kill switch | built |
| Risk: RTH gate | built — `trading/gates.py`, computed NYSE calendar with early closes |
| Liquidity pre-trade gate | built — `trading/gates.py`, armed by `api/deps.py` and pinned there by test |
| Execution service, fill-aware entry, protective stop | built |
| Order state machine | built — `IntentStatus`, 11 states, explicit transition table |
| Idempotency | built — durable `OrderIntent`, unique `idempotency_key` |
| Partial-fill handling | built — actual filled quantity is protected and booked |
| Reconciliation | built — intents, positions, protective orders, structured report |
| Backtest: costs, metrics, walk-forward, benchmark | built |
| Confirmation mode | built |
| Autopilot | enum only — no execution path, correctly disabled |
| Audit, structured logging, kill-switch durability | built |
| AI observability | missing — no agent calls an LLM yet |
| Broker health monitor / fail-safe mode | partial — readiness probe only |
| Memory layers | partial — trade journal only |
| News event taxonomy, SEC EDGAR fundamentals | missing |
| Market-data licensing metadata | missing |
| IBKR adapter | partial — adapter, instrument resolver, and production-shaped `ib_async` transport exist and are contract-tested against fakes; never connected to an IB Gateway |
| Live quote feed | partial — `QuotePort` and an Alpaca implementation exist; the gate fails closed without one |

### Resolved capital-safety defect

`ExecutionService` used to treat a fill timeout as "nothing happened", so a
partially filled entry survived the cancel as an unhedged position. It now
cancels the remainder, re-reads the broker, and carries the actual filled
quantity through the normal protected path. If the broker cannot be read, the
intent becomes `UNKNOWN`, the opportunity stays claimed, and the symbol is
blocked rather than offered for a second entry.

### Execution-state model

`IntentStatus` is Traido's own lifecycle and is wider than any broker's, because
it has to represent "we do not know". Broker statuses are normalized into it by
`trading/order_intent.intent_status_for`; no native broker string reaches the
strategy, risk, or position layers.

The spec for this stage spells the cancelled state `CANCELLED`. The codebase
already had `OrderStatus.CANCELED`, so both use that spelling: two words
differing by one letter, in one codebase, is a defect waiting to be typed.

One rule deserves its own line: a broker order reported as cancelled or expired
*with a non-zero filled quantity* normalizes to `PARTIALLY_FILLED`, never to
`CANCELED`. Those shares exist.

---

## 15. Open decisions

Recorded rather than silently resolved, because each contradicts a current
lock:

1. ~~**Broker.**~~ **Resolved 2026-08-30: IBKR.** IBKR Paper for testing, IBKR
   Live for production, behind `BrokerPort`. Alpaca remains a working adapter
   and stays until the IBKR adapter passes the same lifecycle tests — the
   migration must never leave the desk without a proven execution path.
   Market data stays on Alpaca; that is a separate decision.
2. **Frontend.** The desk is built in Vite + React against locked design tokens.
   The v1.0 stack names Next.js.
3. **Repository layout.** Current layout is flat top-level packages. The v1.0
   layout nests under `apps/` and `core/`.
4. **Workflow engine.** Currently an asyncio scanner loop. The v1.0 stack names
   Temporal.
5. **Numerics.** Quant is dependency-free `Decimal`. The v1.0 stack names
   NumPy/Pandas/Polars, which is right for research and wrong for the order path.
6. ~~**Entry price.**~~ **Resolved 2026-08-31: cross the offer.** The strategy's
   `entry` is `min(SMA20, close)` and the strategy requires an uptrend, so the
   card's price sits below the market by construction; execution holds a limit
   for eighteen seconds because approval is a synchronous request. Held that
   way the two cancel each other out, which a live paper session confirmed on
   the first real order. The limit is now priced at the offer plus a bounded
   buffer and re-sized by the risk engine at that price. The card's `entry`
   remains the strategy's level — it is what the stop and the target are drawn
   from, and what a resting-order execution model would use if the desk ever
   gets one.
7. ~~**Exit signal basis.**~~ **Resolved 2026-08-31: the series the entry was
   drawn on.** The strategy prices a card from the intraday snapshot
   `_confluence` selects; the position agent judged every exit on the daily
   series. Two different SMA20s wearing one name, and the same paper session
   produced a sell proposal eighteen seconds after the fill because the daily
   condition had been true all along. `TradeCandidate.exec_timeframe` now
   carries the timeframe alongside the geometry it produced, and the exit reads
   it. Positions opened before it was recorded fall back to daily.

   The open half: the strategy still *selects* its execution timeframe per scan,
   so two cards on one symbol can be drawn on different series. That is fine for
   the exit now that each carries its own, but it means "the strategy's
   timeframe" is not a single fact about the desk, and any cross-position
   analytics that assumes it is will be wrong.
8. ~~**How much the entry may drift from its card.**~~ **Resolved 2026-08-31: a
   quarter of the planned risk.** Decision 6 made the entry cross the offer,
   which fixed the fills and opened this: the stop and the target do not move
   with the price, so paying up lengthens the risk and shortens the reward at
   once. OXY was drawn at 2:1 and bought at 0.32:1 with nothing objecting.

   The natural re-check — demand the strategy's own 2:1 after repricing — cannot
   be used, and the reason is worth stating because it looks like the safe
   answer. The strategy constructs its target at exactly two times risk, so a
   card reads 2.0 and never more, and any upward repricing at all puts the
   result below it: that rule refuses every entry the desk would ever take. The
   bar that survives is on the entry itself, `MAX_ENTRY_SLIPPAGE_R`, which says
   the same thing the doctrine wanted — past this line it is not the setup that
   was analysed — and yields a worst admissible trade of 1.4:1 as arithmetic
   rather than as a second number to maintain.

   The open half: the desk still shows the card's ratio while the market moves
   under it, so an operator can approve something that will be refused. The
   refusal is safe and named, but the card is stale between the scan and the
   click, and pricing every open card on every poll is a different design.
9. **Who decides an exit, and on what.** Two exits exist and they are not
   symmetric. The protective stop rests at the broker and needs nobody. Everything
   else — the agent's proposal, the operator's close — is a market order sized
   from broker truth at the moment it is pressed, which means the desk has no
   resting take-profit: the target is a number the position agent reasons about,
   not an order at the venue. If the price touches the target while nothing is
   running, nothing happens.

   Left open deliberately rather than closed with an OCO bracket. A broker-side
   take-profit changes what reconciliation is reading — two resting orders per
   position, one of which cancels the other, and a partial fill on either leaves
   a bracket that no longer matches the position it guards. That is a change to
   the invariant the sweep is built on ("a SELL beyond what the venue holds is
   cancelled"), not an addition to it.

---

## 16. First working version

One complete cycle, end to end, before breadth:

```
US market data → scanner → quant/technical → strategy → risk
    → TRADE OPPORTUNITY → approval → PAPER BUY
    → position monitoring → SELL recommendation → approval → PAPER SELL
    → P&L → review
```

News, fundamentals, macro and additional strategies come after that cycle is
clean.
