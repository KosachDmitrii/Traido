# Traido

**Analyze. Decide. Trade.**

Personal AI trading desk for US equities. **Paper trading only in V1.**  
Default mode: **Confirmation** — the system proposes; you approve BUY / discretionary SELL.

> This software will eventually sit next to real capital. Architecture assumes **distrust of LLMs on the money path**: deterministic Risk Engine, broker isolation, append-only audit, paper-first.

## Current stage

**Stage 6 — Vite + React desk** (`frontend/`) on locked soft-UI tokens.  
Python backend API on `:8000`; frontend on `:3000`.

```bash
# Backend
cd backend
../.venv312/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

# Frontend
cd frontend && ./npm.sh install && ./npm.sh run dev
# → http://127.0.0.1:3000
```

See [`backend/README.md`](backend/README.md) and [`frontend/README.md`](frontend/README.md).

**Railway (production):** [`docs/deploy/railway.md`](docs/deploy/railway.md) — separate `backend` + `frontend` services, Postgres, Redis.

`:8000/` redirects to the desk. Use the API on `:8000/api/v1/*`.

```bash
# Force one automatic universe pass
curl -X POST http://127.0.0.1:8000/api/v1/scanner/run

# Desk queue (proposals only)
curl http://127.0.0.1:8000/api/v1/desk
```

Watchlist: `configs/watchlist.json`

| Doc | Path |
|-----|------|
| Vendor lock | [`docs/architecture/vendor-lock.md`](docs/architecture/vendor-lock.md) |
| Architecture | [`docs/architecture.md`](docs/architecture.md) |
| Event flow | [`docs/architecture/event-flow.md`](docs/architecture/event-flow.md) |
| DB schema | [`docs/architecture/database-schema.md`](docs/architecture/database-schema.md) |
| Agents & tools | [`docs/architecture/agents-and-tools.md`](docs/architecture/agents-and-tools.md) |
| Staged plan | [`docs/architecture/staged-plan.md`](docs/architecture/staged-plan.md) |
| Design system | [`docs/design/DESIGN.md`](docs/design/DESIGN.md) · soft UI tokens locked |
| Color tokens | [`docs/design/tokens.css`](docs/design/tokens.css) |

## Safety (non-negotiable)

1. LLM never places orders  
2. LLM never runs raw SQL  
3. Risk Engine is code, not an agent  
4. Paper broker only until Stage 7 gate  
5. Every decision is audit-logged  

## Stack

Python 3.12 · FastAPI · PostgreSQL · Redis · SQLAlchemy 2 · Alembic · Pydantic v2 · Docker Compose  
UI (Stage 6): Vite + React using design tokens above.

## Quick start (Stage 0)

```bash
cp .env.example .env
docker compose up -d db redis
cd backend
/opt/homebrew/bin/python3.12 -m venv ../.venv && source ../.venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn api.main:app --reload
```

Requires **Python 3.12+**.

## Design

Warm soft UI (Cabin/MedSync): accent `#FFCF88`, taupe `#B5A18B`, canvas `#E4E0E0`, ink `#201F1E`.  
See [`docs/design/DESIGN.md`](docs/design/DESIGN.md).

Health: `GET http://localhost:8000/health` → must show `broker_env: paper`, `live_trading: false`.

## Design references

Place UI screenshots in `docs/design/references/` so Stage 6 matches your intended look exactly.

## What happens next

After you approve Stage 0 → **Stage 1**: market data + quant engine (no LLM trading yet).
