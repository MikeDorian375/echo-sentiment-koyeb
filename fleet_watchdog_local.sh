#!/bin/bash
# Echo fleet watchdog (bare-metal / local variant) — restarts dead components.
# Portable: reads state/tunnel_token.txt, uses repo-local config if present.
# Run: bash fleet_watchdog_local.sh  (usually started by start_all.sh)
cd "$(dirname "$0")" || exit 1

VENV=.venv/bin
CF_TUNNEL_ID="d4c987e1-a617-46c7-ae5d-fd3cd9ebe4bb"
CF_URL="${CF_URL:-https://api.6766587364.lol}"
PAY_TO_SOL="${PAY_TO_SOL:-7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk}"
LOG() { echo "[$(date +%H:%M:%S)] $*"; }

# Prefer repo-local cloudflared config, then system config
if [ -f ./cloudflared/config.yml ]; then
  CF_CONFIG=./cloudflared/config.yml
elif [ -f /root/.cloudflared/config.yml ]; then
  CF_CONFIG=/root/.cloudflared/config.yml
else
  CF_CONFIG=./cloudflared/config.yml
fi

while true; do
  # 1. Cloudflare Tunnel
  if ! curl -s -m 5 -o /dev/null "$CF_URL/v1/sample"; then
    pgrep -f "cloudflared tunnel" >/dev/null || {
      LOG "cloudflared dead -> restart"
      TOKEN="${TUNNEL_TOKEN:-$(cat state/tunnel_token.txt 2>/dev/null)}"
      if [ -n "$TOKEN" ]; then
        nohup cloudflared tunnel --config "$CF_CONFIG" run --token "$TOKEN" > cloudflared_wd.log 2>&1 &
      else
        nohup cloudflared tunnel --config "$CF_CONFIG" run "$CF_TUNNEL_ID" > cloudflared_wd.log 2>&1 &
      fi
    }
  fi

  # 2. api_server (:8000) — the paywall
  if ! curl -s -m 3 -o /dev/null http://127.0.0.1:8000/; then
    pgrep -f "api_server.py" >/dev/null || {
      LOG "api dead -> restart"
      nohup env SELF_FACILITATE=1 NETWORK=eip155:8453 PAY_TO_SOL="$PAY_TO_SOL" PUBLIC_BASE="$CF_URL" \
        $VENV/python api_server.py > api.log 2>&1 &
    }
  fi

  # 3. mcp_bridge (:8010)
  if ! curl -s -m 3 -o /dev/null http://127.0.0.1:8010/; then
    pgrep -f "uvicorn mcp_bridge" >/dev/null || {
      LOG "mcp_bridge dead -> restart"
      nohup $VENV/python -m uvicorn mcp_bridge:mcp_app --host 127.0.0.1 --port 8010 --log-level warning > mcp_bridge.log 2>&1 &
    }
  fi

  # 4. clawmerchants push loop (only if keys present)
  if [ -f state/clawmerchants_key.txt ] && [ -f state/attestation_key.txt ]; then
    pgrep -f "clawmerchants_push.py --loop" >/dev/null || {
      LOG "clawmerchants push dead -> restart"
      nohup $VENV/python clawmerchants_push.py --loop > clawmerchants_push.log 2>&1 &
    }
  fi

  sleep 30
done
