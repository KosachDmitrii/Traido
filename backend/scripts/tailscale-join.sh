#!/bin/sh
# Join container to Tailscale (userspace + SOCKS5 for outbound TCP to tailnet IPs).
set -e

if [ -z "${TAILSCALE_AUTHKEY:-}" ]; then
  echo "tailscale: TAILSCALE_AUTHKEY is not set — skipping tailnet join"
  exit 0
fi

KEY=$(printf '%s' "$TAILSCALE_AUTHKEY" | tr -d '[:space:]')
STATE_DIR="${TS_STATE_DIR:-/var/lib/tailscale}"
HOSTNAME="${TS_HOSTNAME:-traido-backend}"
mkdir -p "$STATE_DIR"
echo "tailscale: starting (key length ${#KEY}, state ${STATE_DIR}/tailscaled.state)..."
tailscaled \
  --tun=userspace-networking \
  --socks5-server=127.0.0.1:1055 \
  --statedir="$STATE_DIR" \
  --state="${STATE_DIR}/tailscaled.state" &
sleep 5
if ! tailscale up \
  --auth-key="$KEY" \
  --hostname="$HOSTNAME" \
  --accept-dns=false \
  --reset 2>&1; then
  echo "tailscale: up failed — check TAILSCALE_AUTHKEY (Reusable, same tailnet)"
  exit 1
fi
echo "tailscale: backend node $(tailscale ip -4 2>/dev/null || echo '?')"
