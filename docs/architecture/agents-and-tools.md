# Agent Responsibilities & Tool Contracts

## Boundary rule

| Actor | May call | Must not call |
|-------|----------|---------------|
| Any LLM agent | Read tools returning validated schemas | `BrokerPort.place_order`, raw SQL, shell, arbitrary HTTP |
| Supervisor | Sub-agent tools, quant feature tool, settings read | Execution |
| Execution Service | `BrokerPort`, audit, DB repos | LLM |
| Risk Engine | Portfolio snapshot, limits config | LLM |

Tool results are always JSON matching a named Pydantic model. On validation failure → `SCHEMA_INVALID` audit, pipeline continues or aborts per supervisor policy (default: skip symbol).

---

## Tools (V1 catalog)

### `get_feature_snapshot(symbol, timeframes[]) → FeatureSnapshot[]`
- Implemented by Quant Engine over `MarketDataPort`
- No LLM

### `get_technical_assessment(symbol, features) → TechnicalAssessment`
- Technical Agent
- Prompt may only reason over provided features + bars summary stats, not invent OHLCV

### `get_news_assessment(symbol) → NewsAssessment`
- News Agent + news provider adapter

### `get_market_assessment() → MarketAssessment`
- Market Agent + FRED/index context

### `propose_trade(assessments, features) → TradeCandidate`
- Strategy Agent
- Must include non-empty `reasons`
- Geometry validated by schema

### `evaluate_risk(candidate) → RiskDecision`
- **Code tool**, not LLM — wraps `RiskEngine.evaluate`

### `create_opportunity(candidate, risk) → TradeOpportunity`
- Only if `risk.verdict == pass`
- Service layer

### `submit_user_decision(opportunity_id, decision) → …`
- API / notification webhook
- APPROVE path calls Execution Service (code)

### `propose_exit(position_id) → ExitProposal`
- Position Agent

### `record_audit(event) → void`
- All layers

---

## Prompt versioning

Every LLM call stores:
- `model_name`
- `prompt_version` (semver string in repo, e.g. `technical@0.1.0`)
- `schema_name`

Review Agent groups journal rows by these fields.

---

## Agent I/O summary

```
TechnicalAssessment  score 0–100 + reasons
NewsAssessment       sentiment + score + headlines
MarketAssessment     regime + risk_posture
TradeCandidate       symbol/action/entry/stop/target/RR/confidence
RiskDecision         pass|reject + sized_qty
TradeOpportunity     candidate + risk + status
ExitProposal         SELL|HOLD recommendation
```
