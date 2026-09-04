#!/bin/sh
# Join container to Tailscale (userspace + SOCKS5 for outbound TCP to tailnet IPs).
set -e

if [ -z "${TAILSCALE_AUTHKEY:-}" ]; then
  echo "tailscale: TAILSCALE_AUTHKEY is not set — skipping tailnet join"
  exit 0
fi

KEY=$(printf '%s' "$TAILSCALE_AUTHKEY" | tr -d '[:space:]')
echo "tailscale: starting (key length ${#KEY})..."
tailscaled \
  --tun=userspace-networking \
  --socks5-server=127.0.0.1:1055 \
  --state=mem: &
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
