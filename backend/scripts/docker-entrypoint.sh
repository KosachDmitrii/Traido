#!/bin/sh
# Join Railway backend to your Tailscale network so it can reach IB Gateway on your Mac.
# Set TAILSCALE_AUTHKEY on the backend service (Reusable key from tailscale.com/admin/settings/keys).
set -e

if [ -z "${TAILSCALE_AUTHKEY:-}" ]; then
  echo "tailscale: TAILSCALE_AUTHKEY is not set — skipping tailnet join"
else
  KEY=$(printf '%s' "$TAILSCALE_AUTHKEY" | tr -d '[:space:]')
  echo "tailscale: starting (key length ${#KEY})..."
  tailscaled --tun=userspace-networking --state=mem: &
  sleep 5
  if ! tailscale up \
    --auth-key="$KEY" \
    --hostname="traido-backend" \
    --accept-dns=false \
    --reset 2>&1; then
    echo "tailscale: up failed — check TAILSCALE_AUTHKEY (Reusable, same tailnet)"
    exit 1
  fi
  echo "tailscale: backend node $(tailscale ip -4 2>/dev/null || echo '?')"
fi

exec runuser -u traido -- "$@"
