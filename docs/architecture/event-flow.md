# Event Flow

Correlation id: `pipeline_run_id` (UUID) on every audit row for a scan→trade chain.

## BUY (Confirmation Mode)

```
ScanJobStarted
  → BarsFetched
  → FeaturesComputed          # quant only
  → TechnicalAssessmentReady
  → NewsAssessmentReady
  → MarketAssessmentReady
  → TradeCandidateProposed    # Strategy Agent
  → RiskDecisionRecorded      # PASS | REJECT (code)
      ├─ REJECT → OpportunityDiscarded → END
      └─ PASS → OpportunityCreated (awaiting_confirmation)
           → ConfirmationRequested
               ├─ SKIP → OpportunitySkipped → END
               ├─ DETAILS → OpportunityDetailsViewed (no state change)
               └─ APPROVE → OpportunityApproved
                    → OrderSubmitted (entry)
                    → OrderSubmitted (protective_stop)
                    → FillReceived*
                    → PositionOpened
                    → (Position Agent loop)
                         → ExitProposed (optional)
                              ├─ HOLD → continue
                              └─ SELL approved → OrderSubmitted (close)
                                   → FillReceived
                                   → PositionClosed
                                   → TradeJournalFinalized
```

## Protective stop (no extra human tap)

After `PositionOpened`, stop modifications by Execution Service are allowed when:
- volatility expansion requires stop widen/tighten per policy, **or**
- broker rejects/requires replace

Always: `StopOrderUpdated` audit event. Never remove stop without kill-switch policy review.

## AUTOPILOT (future, disabled)

Same as BUY until `OpportunityCreated`, then skip ConfirmationRequested and go to `OpportunityAutoApproved` **only if**:
- `trading.mode == autopilot`
- kill switch off
- Risk PASS
- paper evaluation gate satisfied (config flag)

## Failure classes

| Code | Meaning |
|------|---------|
| `SCHEMA_INVALID` | Agent output failed Pydantic |
| `RISK_REJECT` | Hard limit |
| `BROKER_REJECT` | Broker refused order |
| `DATA_STALE` | Bars too old for signal |
| `KILL_SWITCH` | Global halt |
| `IDEMPOTENCY_HIT` | Duplicate client order id ignored |
