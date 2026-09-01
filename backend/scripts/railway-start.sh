#!/bin/sh
# Railway sets PORT per deployment. Default 8000 for local Docker.
set -e
PORT="${PORT:-8000}"

# Schema must exist before the API boots (init_db refuses a drifted Postgres).
# Pre-deploy may also run this; doing it here covers first boot and failed hooks.
alembic upgrade head

exec uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
