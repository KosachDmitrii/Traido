# Runtime path — Alpaca Paper

The retired audit snapshot remains in Git history. The current execution path is:
market data → strategy → admission/risk → manual approval → durable order intent
→ Alpaca Paper → reconciliation → ledger and protective exits.

`backend/broker/factory.py` constructs the sole Paper adapter.
`backend/api/deps.py` composes execution services with their mandatory gates.
`backend/broker/journal_guard.py` prevents reconciling foreign operational state.
`backend/risk/paper_period.py` persists account-scoped observed risk.

See [architecture](../../ARCHITECTURE.md), [migration](alpaca-only.md), and
[failure matrix](execution-failure-matrix.md). This provider migration does not
claim a new comprehensive audit of unrelated runtime paths.
