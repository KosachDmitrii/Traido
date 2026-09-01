#!/bin/sh
# Railway sets PORT per deployment. Default 8000 for local Docker.
set -e
PORT="${PORT:-8000}"
exec uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
