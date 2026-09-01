# Traido Architecture — Stage 0

**Product:** Traido — Analyze. Decide. Trade.  
**Version:** 0.1.0-architecture  
**Status:** Superseded as the top-level view by `ARCHITECTURE.md` (v1.0 freeze).
Retained for the Stage 0 contracts and rationale, which remain in force.  
**Capital mode (V1):** PAPER TRADING ONLY  
**Default trade mode:** CONFIRMATION (human must approve BUY and discretionary SELL)

---

## 0. Why this document exists

Traido will eventually touch real money. Stage 0 exists to make irreversible mistakes impossible by design:

| Principle | Enforcement |
|-----------|-------------|
| LLMs never move money | Execution path is code-only; LLM output is a proposal schema |
| LLMs never touch the database with raw SQL | Agents call typed repositories / tools only |
| Risk is not negotiable | `RiskEngine` is deterministic Python; no LLM in that module |
| Paper before live | `BrokerEnvironment` enum; live adapter not wired in V1 |
| Every decision is auditable | Append-only `audit_events` for signals, tool calls, risk, orders |
| Human is in the loop by default | `TradingMode.CONFIRMATION`; Autopilot is explicit + gated |

**Profitability is not assumed.** The system must prove expectancy on journaled paper trades before live capital is considered.

---

## 1. Product scope (V1)

### In scope
- US equities, long-only
- Multi-timeframe technical + quant analysis (Python)
- News + market regime context (agents)
- Strategy proposals with entry / stop / target / R:R
- Deterministic risk checks and position sizing
- Human confirmation of BUY and discretionary SELL
- Automatic protective stop placement after approved entry
- Paper broker (IBKR Paper or Alpaca Paper via adapter)
- Full trade journal for later Review Agent
- Audit log of the entire decision chain

### Explicitly out of scope (V1)
- Live trading / funded account
- Autopilot as default (flag exists, disabled)
- Leverage, margin, short selling, options, futures, crypto
- Polished production frontend (design system is specified; UI ships Stage 6)
- Expensive real-time feeds as a hard dependency (Massive optional later)
- Full Fundamental Agent depth (SEC wiring reserved for V2; schema placeholders ok)

---

## 2. High-level system diagram

```
                    ┌─────────────────────────────────────┐
                    │         TRAIDO CORE (API)           │
                    │  FastAPI · Postgres · Redis · Audit │
                    └─────────────────┬───────────────────┘
                                      │
                              Supervisor Agent
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            ▼                         ▼                         ▼
      Technical Agent           News Agent              Market Agent
            │                         │                         │
            └────────────┬────────────┴────────────┬────────────┘
                         ▼                         │
                   Quant Engine ◄── market_data    │
                   (deterministic)                 │
                         │                         │
                         └────────────┬────────────┘
                                      ▼
                              Strategy Agent
                           (TradeCandidate only)
                                      ▼
                         ┌────────────────────────┐
                         │   RISK ENGINE (CODE)   │  ← never LLM
                         │  size · limits · kill  │
                         └────────────┬───────────┘
                                      ▼
                            TradeOpportunity
                                      ▼
                    ┌─────────────────┴─────────────────┐
                    │  CONFIRMATION (default)            │
                    │  user: APPROVE | DETAILS | SKIP    │
                    └─────────────────┬─────────────────┘
                                      ▼
                            Execution Service
                         (orders via BrokerPort)
                                      ▼
                              Paper Broker
                                      ▼
                             Position Agent
                         (SELL proposal → confirm)
                                      ▼
                              Review Agent
                         (post-trade analytics)
```

**Critical split:** Market data provider ≠ broker. Scan/analyze on a data provider; account/orders on broker. Interfaces allow swapping either without rewriting agents.

---

## 3. Module map

| Path | Responsibility | LLM? |
|------|----------------|------|
| `agents/` | Orchestration + interpretation of precomputed features | Yes (bounded) |
| `quant/` | Indicators, patterns, S/R, regime features, backtest | **No** |
| `risk/` | Limits, sizing, kill switch, exposure | **No** |
| `broker/` | Account, orders, fills (paper first) | **No** |
| `market_data/` | OHLCV, quotes (provider adapters) | **No** |
| `trading/` | Signals, opportunities, order domain services | **No** |
| `api/` | HTTP: health, opportunities, confirm, portfolio | **No** (calls services) |
| `database/` | SQLAlchemy models, repositories, Alembic | **No** |
| `core/` | Config, enums, schemas, security, audit helpers | **No** |

---

## 4. Critical safety rules (non-negotiable)

1. **LLMs must NEVER execute raw SQL.**
2. **LLMs must NEVER call broker place/cancel/modify APIs directly.**
3. **All agent outputs MUST validate against Pydantic v2 schemas** (`core/schemas/`). Failure = reject + audit.
4. **Risk Engine is deterministic code.** Supervisor cannot override a `REJECT`.
5. **Broker execution happens only after** Risk approval **and** (Confirmation Mode: human approve | Autopilot Mode: autopilot flag enabled + risk pass).
6. **V1 default:** every BUY and discretionary SELL requires human approval.
7. **Protective stops** may be placed/updated by Execution Service without a new tap (still audit-logged).
8. **No leverage, options, short selling, or margin in V1.**
9. **Every signal, tool call, decision, order, and fill is audit-logged** (append-only).
10. **Backtesting + paper trading required** before any live adapter is enabled.
11. **Provider interfaces** (`BrokerPort`, `MarketDataPort`, `LLMPort`) allow replacement without agent rewrites.
12. **Frontend is not implemented in Stages 0–5**; design tokens live in `docs/design/` for Stage 6.

### Kill switch

`risk.kill_switch` (Redis + DB flag) immediately:
- rejects new opportunities
- blocks new orders
- does **not** cancel protective stops already working (configurable; default: leave stops)

---

## 5. Trading modes

```text
CONFIRMATION (default)
  TradeCandidate → Risk PASS → Opportunity queued
  → push/notify user → APPROVE | SKIP | DETAILS
  → on APPROVE → Execution Service → Broker

AUTOPILOT (disabled until explicit enable + paper evaluation)
  TradeCandidate → Risk PASS → Execution Service → Broker
  Still subject to kill switch and hard limits
```

Settings key: `trading.mode` ∈ {`confirmation`, `autopilot`}.

---

## 6. Agent responsibilities (V1)

### Supervisor Agent
- Owns scan cycle orchestration
- Invokes tools / sub-agents in order
- Merges structured results; never invents prices
- Emits pipeline run id for audit correlation
- Cannot place orders; cannot bypass Risk

### Technical Agent
- **Input:** precomputed multi-TF features from Quant Engine (not raw chart images as primary path)
- Timeframes: `1D`, `4H`, `1H`, `15m` (5m optional later)
- **Output:** `TechnicalAssessment` — trend, S/R, patterns, RSI/MACD context, score 0–100, reasons[]

### News Agent
- Fetches recent headlines/events for symbol + market
- **Output:** `NewsAssessment` — sentiment, material events, score/label, citations

### Market Agent
- Regime / breadth / macro snapshot (FRED + index context)
- **Output:** `MarketAssessment` — regime label, risk-on/off, notes

### Strategy Agent
- Combines Technical + Quant + News + Market
- **Output only:** `TradeCandidate` (proposal). Never an order.

### Position Agent
- Watches open positions vs stop/target/structure/news
- **Output:** `ExitProposal` (SELL/HOLD recommendation). Discretionary exits need confirm in V1.

### Review Agent
- Offline / scheduled: aggregates journaled trades
- Ranks setups, prompt/strategy versions, failure modes
- **No trading authority**

### Quant Engine (not an agent)
- Pure functions over OHLCV → indicators, candlestick patterns, chart patterns, S/R, volume metrics
- Same code path for live scan and backtest

### Risk Engine (not an agent)
- Validates candidate + portfolio state → `RiskDecision` (PASS/REJECT + sized qty + reasons)
- Position sizing from stop distance and equity

### Execution Service (not an agent)
- Translates approved opportunity → broker orders (entry + protective stop)
- Idempotent client order ids
- Reconciles fills into journal

---

## 7. Event flow (happy path BUY)

1. `ScanJobStarted` (supervisor)
2. Market data fetch → bars cached
3. Quant features computed → `FeaturesComputed`
4. Technical / News / Market assessments → validated schemas
5. Strategy emits `TradeCandidateProposed`
6. Risk Engine → `RiskDecisionRecorded` (PASS | REJECT)
7. If PASS → `OpportunityCreated` (status=`awaiting_confirmation`)
8. User notified → `ConfirmationRequested`
9. User APPROVE → `OpportunityApproved`
10. Execution → `OrderSubmitted` (entry) → `OrderSubmitted` (stop)
11. Fill(s) → `FillReceived` → `PositionOpened`
12. Position Agent loop → optional `ExitProposed` → confirm → `PositionClosed`
13. `TradeJournalFinalized` → Review metrics updated

Reject / skip paths always write audit events with reason codes.

---

## 8. Data sources (V1 wiring intent)

| Need | V1 | Later |
|------|----|-------|
| OHLCV | Broker historical and/or delayed/cheap provider | Massive / licensed RT |
| Indicators | `quant/` | same |
| Macro | FRED | same |
| News | Single news provider adapter | multi-source |
| Fundamentals | Stub / optional SEC later | EDGAR XBRL |
| Portfolio / orders | IBKR Paper **or** Alpaca Paper | Live IBKR |

**Cost discipline:** do not bind V1 to $199/mo feeds. Interfaces must accept a richer provider later without agent changes.

---

## 9. Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12 |
| API | FastAPI |
| Validation | Pydantic v2 |
| DB | PostgreSQL 16 |
| ORM | SQLAlchemy 2.x |
| Migrations | Alembic |
| Queue / state | Redis 7 |
| Containers | Docker Compose |
| UI (Stage 6) | Vite + React (`frontend/`) — see `docs/design/` |
| LLM | Claude via `LLMPort` (provider-swappable) |

---

## 10. Configuration & secrets

- Secrets only via env / secret manager — never committed
- `.env.example` lists required keys without values
- Separate configs: `paper` vs (future) `live`
- `TRAIDO_BROKER_ENV=paper` hard-checked at startup; live refused unless build flag + explicit env

---

## 11. Testing requirements (architecture-level)

| Layer | Must cover |
|-------|------------|
| Quant | Golden fixtures for RSI/MACD/patterns |
| Risk | Limit breaches, sizing math, kill switch |
| Broker adapter | Mocked paper place/cancel/reconcile |
| Agents | Schema validation; tool-boundary tests (no broker calls) |
| API | Confirm/skip flows; unauthorized execution attempts fail |
| Integration | Scan → opportunity → approve → mock fill → journal |

**No test may place a live order.**

---

## 12. Design system binding

UI is deferred to Stage 6, but **visual language is locked** from user-approved references:

- Spec: [`docs/design/DESIGN.md`](design/DESIGN.md)
- Tokens: [`docs/design/tokens.css`](design/tokens.css)
- References: [`docs/design/references/`](design/references/) (Cabin palette + MedSync soft UI)

**Palette lock (Cabin):** accent `#FFCF88` · taupe `#B5A18B` · canvas `#E4E0E0` · ink `#201F1E`

**UI language:** warm soft UI — floating white cards, large radii (~24px), pill nav/search, mustard for CTA/highlights, charcoal for high-contrast risk/SELL blocks, soft area/bar charts. Not a dark neon terminal.

Dashboard IA:
- Soft sidebar + pill active state
- Portfolio equity + today/total P&L as stat tiles with trend pills
- Agents / positions lists
- Opportunities as mustard/ink schedule-style blocks
- Confirm: BUY (mustard) / DETAILS (ink) / SKIP (ghost) · SELL / HOLD
- PAPER chip always in header

---

## 13. Staged implementation (after this freeze)

| Stage | Deliverable | Exit criteria |
|-------|-------------|---------------|
| **0** | This architecture + skeleton + contracts | Reviewed & approved |
| **1** | Market data port + Quant engine | Fixture tests green |
| **2** | Backtest harness + journal models | Replay produces journal rows |
| **3** | Agents V1 + Supervisor | Valid TradeCandidates only |
| **4** | Risk + Confirm API + Paper broker | Approve → paper order |
| **5** | Position + Review + audit completeness | Exit proposals + analytics |
| **6** | Next.js dashboard + notifications | Matches design tokens |
| **7** | Paper evaluation period → optional Autopilot → live decision gate | Documented expectancy |

---

## 14. Vendor decisions — LOCKED

See [`docs/architecture/vendor-lock.md`](architecture/vendor-lock.md).

| Concern | Choice |
|---------|--------|
| Paper broker | **Alpaca Paper** |
| Live broker (future) | **IBKR** (Stage 7+) |
| OHLCV V1 | **Alpaca Market Data** |
| Scale scan (future) | Massive (optional) |
| News | **Finnhub** |
| Macro | **FRED** |
| Confirm notify | **Telegram Bot** |
| LLM | **Anthropic Claude** |
| Design | Cabin/MedSync soft UI |

---

## 15. Review checklist

- [x] Safety rules accepted (owner: proceed with professional lock)
- [x] Module boundaries accepted
- [x] V1 scope / out-of-scope accepted
- [x] Confirm-first flow accepted
- [x] Design tokens accepted (Cabin/MedSync soft UI locked)
- [x] Vendor stack locked (Alpaca paper + data, IBKR live later, Finnhub, FRED, Telegram)
- [x] Stage 1 authorized

**Stage 0 freeze complete. Stage 1 in progress.**
