#!/bin/bash
# Container fleet watchdog — restarts dead components. Koyeb's container
# runtime reaps background processes, so this loop keeps the stack alive.
# Run: started by entrypoint.sh
cd "$(dirname "$0")" || exit 1
LOG() { echo "[$(date +%H:%M:%S)] $*"; }

while true; do
  # 1. Cloudflare Tunnel (same tunnel as phone -> failover)
  if ! curl -s -m 5 -o /dev/null https://api.6766587364.lol/v1/sample; then
    pgrep -f "cloudflared tunnel" >/dev/null || {
      LOG "cloudflared dead -> restart"
      if [ -n "$TUNNEL_TOKEN" ]; then
        nohup cloudflared tunnel --config /etc/cloudflared/config.yml run --token "$TUNNEL_TOKEN" > cloudflared.log 2>&1 &
      else
        nohup cloudflared tunnel --config /etc/cloudflared/config.yml run > cloudflared.log 2>&1 &
      fi
    }
  fi

  # 2. api_server (:8000) — the paywall
  if ! curl -s -m 3 -o /dev/null http://127.0.0.1:8000/; then
    pgrep -f "python api_server.py" >/dev/null || {
      LOG "api dead -> restart"
      nohup env SELF_FACILITATE=1 NETWORK=eip155:8453 PAY_TO_SOL="${PAY_TO_SOL:-7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk}" \
        PUBLIC_BASE="${PUBLIC_BASE:-https://api.6766587364.lol}" \
        python api_server.py > api.log 2>&1 &
    }
  fi

  # 3. mcp_bridge (:8010)
  if ! curl -s -m 3 -o /dev/null http://127.0.0.1:8010/; then
    pgrep -f "uvicorn mcp_bridge" >/dev/null || {
      LOG "bridge dead -> restart"
      nohup python -m uvicorn mcp_bridge:mcp_app --host 127.0.0.1 --port 8010 --log-level warning > mcp_bridge.log 2>&1 &
    }
  fi

  sleep 30
done
