#!/bin/sh
# Join Railway backend to your Tailscale network so it can reach IB Gateway on your Mac.
# Set TAILSCALE_AUTHKEY on the backend service (one-time key from tailscale.com/admin/settings/keys).
set -e

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  echo "tailscale: starting..."
  tailscaled --tun=userspace-networking --state=mem: &
  sleep 3
  tailscale up \
    --auth-key="$TAILSCALE_AUTHKEY" \
    --hostname="traido-backend" \
    --accept-dns=false \
    --reset
  echo "tailscale: backend node $(tailscale ip -4 2>/dev/null || echo '?')"
fi

exec runuser -u traido -- "$@"
