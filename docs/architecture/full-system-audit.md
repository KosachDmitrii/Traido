# Full system audit — Traido

**Audited:** 2026-08-31 (repository truth, not prior chat claims).  
**Maturity classification:** `PAPER_READY` (not `PAPER_PROVEN` — IBKR Paper E2E unproven; multi-worker claim not DB-CAS until Milestone A closes it).  
**Not:** LIVE · AUTOPILOT · CONTROLLED_AUTOPILOT.

This document is the Milestone 0 deliverable from the production-hardening brief.
Statuses: `PROVEN` | `PARTIALLY_PROVEN` | `UNPROVEN` | `BROKEN` | `NOT_IMPLEMENTED`.

---

## 1. Maturity

| Claim | Classification |
| --- | --- |
| Overall | **PAPER_READY** |
| Capital path (Alpaca Paper / mock) | PARTIALLY_PROVEN → targeting PROVEN in Milestone A–B |
| IBKR Paper | UNPROVEN (adapter coded, Gateway never run) |
| Forward Paper evidence pack | UNPROVEN |
| Shadow mode | NOT_IMPLEMENTED |
| Live / Autopilot | BLOCKED at boot |

---

## 2. Architecture map

```
Market Data (Alpaca) → Quant → Agents (heuristics) → Strategy → Liquidity
  → Risk (deterministic) → Human APPROVE → OrderIntent → ExecutionService
  → BrokerPort → Reconcile → Ledger / Protection → Review
```

Prime directive frozen in `ARCHITECTURE.md` / `AGENTS.md`. LLM may not move money.

---

## 3. Live runtime map (operator machine, post-restart)

| Component | State |
| --- | --- |
| API | FastAPI `:8000`, single worker, reload |
| Frontend | Vite `:3000` |
| Loops | scanner · reconcile ~30s · position ~60s |
| Mode | `confirmation` · `broker_env=paper` · live off |
| Universe | `EXTENDED` · max `2000` · funnel 150→30→20 |
| Finnhub | news + earnings + sector `profile2` |
| Execution broker | Alpaca Paper when keyed; else mock |

---

## 4. Trading flow (new exposure)

`POST /api/v1/opportunities/{id}/decide` → `build_execution_service()` →
`ExecutionService.decide` → claim → risk (+ reprice) → gates → `_entry_intent` →
`_place_entry` → fill → ledger → protection.

Only BUY path that increases exposure. Autopilot refused at boot.

---

## 5. Scanner flow

`UniverseProvider` → Stage0 eligibility → Stage1 batched market prefilter →
Stage2 quant top-K → Stage3 deep (bounded concurrency) → Stage4 Risk →
rank → capacity → publish. Funnel accounting in `STATUS.funnel`.

---

## 6. Data providers

| Port | Adapter | Status |
| --- | --- | --- |
| MarketData / Quotes | Alpaca | PROVEN (paper) |
| News / Earnings / Sector | Finnhub | PARTIALLY_PROVEN (sector source new; fail-closed tested) |
| Macro | FRED | PARTIALLY_PROVEN |
| Broker | Alpaca Paper | PROVEN for paper path |
| Broker | IBKR | UNPROVEN live session |
| LLM | Anthropic | wired; decision path still heuristics |

---

## 7. Strategy logic

Live confluence-style setup (`strategy_confluence@0.2.0` class): D1 trend +
pullback; geometry from exec TF; ~2R target. **No** first-class `Strategy` /
`StrategyVersion` registry → NOT_IMPLEMENTED for promotion evidence.

---

## 8. AI usage

Agents interpret with allowlists documented in `AGENTS.md`. Runtime tool
allowlist enforcement → NOT_IMPLEMENTED (Stage 10). Prompt registry →
NOT_IMPLEMENTED. AI does not call `place_order` (AST-guarded).

---

## 9. Risk logic

`RiskEngine` + `RiskContext` fail-closed on earnings, news, sector, kill switch,
concentration, size, regime. Approval re-derives context. Limits from config,
not prompts. **PROVEN** at unit/integration for listed gates; DecisionPipeline
as declared sequence → NOT_IMPLEMENTED (P1-8).

---

## 10. Broker state machine

`BrokerLinkState`: DISCONNECTED / CONNECTING / READY / DEGRADED / RECONNECTING.
New exposure requires READY. Heartbeat / latency metrics surface → PARTIAL
(state exists; rich ops metrics → Milestone B).

---

## 11. P0 — capital safety

| ID | Issue | Status |
| --- | --- | --- |
| A-P0-1 | Opportunity `claim` is process-local lock, not DB CAS — two workers can both approve | **OPEN** (contained by single-worker) |
| A-P0-2 | Intent `CREATED→SUBMITTING` not compare-and-swap — dual `place_order` possible if claim lost | **OPEN** (same containment) |
| Historical P0s in gap-register | Duplicate stops, reconcile on GET, exit atomicity, secrets, etc. | **CLOSED** |

---

## 12. P1 — trading correctness

| ID | Issue | Status |
| --- | --- | --- |
| P1-8 | DecisionPipeline (declared gate order) | OPEN |
| P1-10 | Displayed vs executed size | OPEN (softened: re-derive on approve) |
| P1-6 / P1-11 | Multi-worker / multi-node | CONTAINED |
| Sector UNCLASSIFIED exposure | Finnhub source + refuse policy | PARTIALLY_PROVEN |
| Finalist freshness refresh before publish | Scan quote age vs exec | PARTIAL |

---

## 13. P2 — operational reliability

Metrics/alerting, SCAN_OVERRUN operator signal, ProtectionVerified UI,
disaster-test pack completeness, corporate actions, MARKET_HALTED.

---

## 14. P3 — maintainability / product

Strategy registry, ranking OpportunityScore, AI observability, repo layout
open decisions, `execution.py` size.

---

## 15. Missing integration proofs

- DB-level concurrent approval across simulated workers
- DecisionPipeline wiring AST + HTTP zero-mutation matrix for every mandatory gate
- IBKR Paper E2E checklist (§41 of brief)
- Crash/restart matrix as one harness
- Shadow / Forward Paper evidence objects

Existing strengths: `tests/integration/test_entry_gates_end_to_end.py`,
lifecycle, races (`test_22`), capital_safety AST, sector/news/earnings fail-closed.

---

## 16. Missing trading evidence

No StrategyVersion evidence chain (backtest → OOS → WF → paper → shadow).
Evaluation page exists; promotion states do not.

---

## 17. Milestone A — files expected to change

- `trading/opportunities.py` — DB CAS claim
- `trading/intents.py` — `transition_from` CAS
- `trading/execution.py` — submit only after won SUBMITTING; wire pipeline
- `trading/decision_pipeline.py` — **new**
- `trading/gates.py` / risk context — gate result shape if needed
- `api/routes` / desk types — proposed vs approved qty (P1-10)
- `frontend` — show approved/executed size when differs
- `tests/integration/` — stress + gate matrix
- `tests/unit/test_capital_safety.py` — pipeline bypass AST
- `docs/architecture/gap-register.md`, `execution-failure-matrix.md`, this file

---

## 18. Database changes expected (A)

No new tables required for claim CAS (`UPDATE … WHERE status=` / `FOR UPDATE`).
Optional later: opportunity `version` column. Intent unique key already present.

---

## 19. Test plan (A)

1. Red-without-fix: break claim CAS → concurrent test fails
2. Stress: N parallel HTTP approvals → 1 BUY (hundreds of iterations)
3. Intent `transition_from`: loser recovers, does not place
4. Gate matrix: missing MD / quote / RTH / earnings / READY / stale reconcile → 0 mutations
5. All PASS → 1 intent + 1 mutation
6. Displayed qty fields present on desk payload
7. Full pytest + ruff + mypy capital path

---

## Milestone A status (2026-08-31)

| Deliverable | Status |
| --- | --- |
| Concurrent approval DB CAS claim | **DONE** — `OpportunityStore.claim` uses `WHERE status` + `FOR UPDATE` |
| Intent submit CAS | **DONE** — `transition_from(CREATED→SUBMITTING)` on entry/exit/protect/emergency |
| Stress tests | **DONE** — `test_concurrent_approval_stress.py` (2/4/8 workers + 50 rounds) |
| DecisionPipeline module | **PARTIAL** — declared `NEW_EXPOSURE_GATE_ORDER` + AST guard; full runner not sole body of `decide()` |
| Displayed vs executed qty | **PARTIAL** — `proposed_qty` / `approved_qty` / `executed_qty` on opportunity + UI |
| Quality gate | **947 passed** |

Next: Milestone B in progress — `core/alerts.py` + reconcile hooks done; desk
health surface and full ops metrics still open. Milestone C (IBKR Paper) remains
BLOCKED without Gateway credentials.

Subsystem classification summary:

| Subsystem | Status |
| --- | --- |
| Risk engine | PROVEN |
| Liquidity / RTH / reconcile age gates | PROVEN (path-armed) |
| OrderIntent + idempotency | PROVEN |
| Protection + emergency | PROVEN (Alpaca/mock) |
| Single-flight reconcile | PROVEN |
| Opportunity claim (cross-process) | PARTIALLY_PROVEN (DB CAS + single-worker) |
| DecisionPipeline | PARTIALLY_PROVEN (order declared; runner not sole decide body) |
| Scanner funnel EXTENDED | PARTIALLY_PROVEN |
| Sector source Finnhub | PARTIALLY_PROVEN |
| Desk UI | PROVEN (paper desk) |
| IBKR | UNPROVEN |
| Strategy registry / evidence | NOT_IMPLEMENTED |
| Metrics / alerts | NOT_IMPLEMENTED / PARTIAL |
| Autopilot | NOT_IMPLEMENTED (boot refuse) |
