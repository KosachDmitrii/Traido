#!/bin/sh
# Railway sets PORT per deployment. Default 8000 for local Docker.
set -e
PORT="${PORT:-8000}"

# Railway bypasses Docker ENTRYPOINT when startCommand is set — join tailnet here.
if [ -n "${TAILSCALE_AUTHKEY:-}" ] && [ "$(id -u)" = "0" ]; then
  /app/scripts/tailscale-join.sh
fi

# Schema must exist before the API boots (init_db refuses a drifted Postgres).
# Pre-deploy may also run this; doing it here covers first boot and failed hooks.
alembic upgrade head

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  # userspace-networking: app TCP must go through Tailscale SOCKS5.
  exec proxychains4 -q uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
fi

exec uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
