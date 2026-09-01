# Traido backend (Python)

FastAPI confirmation desk API and trading pipeline. This is the capital path —
React UI never talks to Alpaca or SQL directly.

## Layout

| Package | Role |
|---------|------|
| `api/` | HTTP routes (`/api/v1/desk`, decide, scanner, …) |
| `trading/` | Execution, ledger, reconcile, opportunities |
| `risk/` | Deterministic Risk Engine + kill switch |
| `broker/` | Alpaca paper / mock |
| `agents/` | Scanner, strategy, position, review |
| `market_data/` | OHLCV adapters |
| `core/` | Schemas, ports, desk bus, config |
| `database/` | SQL models / session |
| `universe/` | Universe provider and eligibility |
| `configs/` | Risk limits, watchlist, universe presets |
| `tests/` | Unit and integration tests |

## Run

```bash
cd backend
../.venv312/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

- Health: http://127.0.0.1:8000/health  
- Desk API: http://127.0.0.1:8000/api/v1/desk  
- UI: Vite app in `../frontend` on `:3000`

Environment file lives at the repo root: `../.env` (copy from `../.env.example`).

## Safety (V1)

- Paper only · no live orders  
- LLM never places orders or writes SQL  
- Orders only through `ExecutionService`
