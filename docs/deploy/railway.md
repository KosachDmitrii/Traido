# Deploy Traido on Railway

Monorepo layout: `backend/` (FastAPI) + `frontend/` (Vite/React).  
Managed **PostgreSQL** and **Redis** live in the same Railway project.

## Architecture

```
┌─────────────┐     HTTPS      ┌─────────────┐
│  frontend   │ ──────────────▶│   backend   │
│  (nginx)    │   API calls    │  (FastAPI)  │
└─────────────┘                └──────┬──────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              ┌──────────┐     ┌──────────┐     ┌──────────┐
              │ Postgres │     │  Redis   │     │  Vendors │
              │ (journal)│     │kill switch│     │ Alpaca…  │
              └──────────┘     └──────────┘     └──────────┘
```

## 1. Create project

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub** → select this repo.
2. **+ New** → **Database** → **PostgreSQL**
3. **+ New** → **Database** → **Redis**

## 2. Backend service

1. **+ New** → **GitHub Repo** → same repository.
2. Rename service to **`backend`** (reference variables use this name).
3. **Settings → Source → Root Directory:** `/backend`
4. **Settings → Networking → Generate Domain** (public URL for the API).
5. **Settings → Deploy:**
   - Config file: `backend/railway.toml` (auto-detected when root is `/backend`)
   - Pre-deploy runs `alembic upgrade head` before each deploy.

### Backend variables

| Variable | Value |
|----------|--------|
| `TRAIDO_ENV` | `production` |
| `TRAIDO_BROKER_ENV` | `paper` |
| `TRAIDO_TRADING_MODE` | `confirmation` |
| `TRAIDO_ALLOW_LIVE_TRADING` | `false` |
| `TRAIDO_API_KEY` | *(generate a long random secret — required in production)* |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `TRAIDO_JOURNAL_DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `TRAIDO_CORS_ORIGINS` | `https://${{frontend.RAILWAY_PUBLIC_DOMAIN}}` |
| `TRAIDO_DASHBOARD_URL` | `https://${{frontend.RAILWAY_PUBLIC_DOMAIN}}` |
| `ALPACA_API_KEY` | *(your Alpaca paper key)* |
| `ALPACA_API_SECRET` | *(your Alpaca paper secret)* |
| `FINNHUB_API_KEY` | *(required for entries unless disabled in config)* |

Replace `Postgres` / `Redis` / `frontend` with your actual service names if different.

## 3. Frontend service

1. **+ New** → **GitHub Repo** → same repository.
2. Rename service to **`frontend`**.
3. **Settings → Source → Root Directory:** `/frontend`
4. **Settings → Networking → Generate Domain**.

### Frontend variables (build-time)

Vite inlines `VITE_*` at **build**, not at runtime:

| Variable | Value |
|----------|--------|
| `VITE_API_BASE_URL` | `https://${{backend.RAILWAY_PUBLIC_DOMAIN}}` |

After changing `VITE_API_BASE_URL`, **redeploy** the frontend so the bundle is rebuilt.

## 4. Deploy order

1. Postgres + Redis (provisioned automatically)
2. **backend** — wait until `/health/ready` is green
3. **frontend** — needs backend public domain for `VITE_API_BASE_URL`

## 5. Verify

```bash
# Backend
curl https://<backend-domain>/health
curl https://<backend-domain>/health/ready

# Desk opens in browser
open https://<frontend-domain>
```

Health must show `broker_env: paper` and `live_trading: false`.

## 6. Optional: persistent `/app/data`

Kill switch and audit file fallbacks use `/app/data`. With Redis configured, kill switch is shared and durable across restarts. For extra file durability:

1. **backend** → **Settings → Volumes** → mount `/app/data`

## Local parity

```bash
docker compose up -d db redis migrate api frontend
```

Same images as Railway (`backend/Dockerfile`, `frontend/Dockerfile`).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Backend refuses to boot | Set `TRAIDO_API_KEY` in production |
| CORS errors in browser | `TRAIDO_CORS_ORIGINS` must match frontend URL exactly (https, no trailing slash) |
| Frontend calls wrong API | Redeploy frontend after fixing `VITE_API_BASE_URL` |
| Schema errors | Check backend deploy logs for `alembic upgrade head` |
| Entries always refused | Set `FINNHUB_API_KEY` or disable earnings check in `configs/v1_paper.json` |
