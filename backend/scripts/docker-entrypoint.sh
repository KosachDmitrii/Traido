#!/bin/sh
# Local Docker: join tailnet as root, then drop to traido for the app.
set -e

if [ -n "${TAILSCALE_AUTHKEY:-}" ] && [ "$(id -u)" = "0" ]; then
  /app/scripts/tailscale-join.sh
fi

exec runuser -u traido -- "$@"
