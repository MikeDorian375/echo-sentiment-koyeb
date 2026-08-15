#!/bin/bash
# Koyeb entrypoint: materialize secrets into state/, start Cloudflare Tunnel
# + api + bridge + watchdog. Same tunnel as the phone -> same public URL,
# Cloudflare load-balances across both connectors (failover for free).
set -e
cd "$(dirname "$0")"
mkdir -p state /root/.cloudflared

# Materialize keys from env (Koyeb secrets) if provided
[ -n "$GAS_KEY_EVM" ]  && echo "$GAS_KEY_EVM"  > state/gas_key.txt
[ -n "$GAS_KEY_SOL" ]  && echo "$GAS_KEY_SOL"  > state/sol_gas_key.txt
[ -n "$TEST_CLIENT_KEY" ] && printf "0x0000000000000000000000000000000000000000\n%s\n" "$TEST_CLIENT_KEY" > state/testnet_client_wallet.txt
[ -n "$PAY_TO_SOL" ]   && export PAY_TO_SOL

# Cloudflare Tunnel (same tunnel ID + credentials as the phone)
if [ -n "$TUNNEL_TOKEN" ]; then
  # Option 1: single token (simplest secret). Ingress comes from config.yml.
  cloudflared tunnel --config /etc/cloudflared/config.yml run --token "$TUNNEL_TOKEN" \
    > cloudflared.log 2>&1 &
  echo "[entry] cloudflared up (token)"
elif [ -n "$TUNNEL_CREDENTIALS" ]; then
  # Option 2: raw credentials JSON ({"AccountTag":...,"TunnelSecret":...,"TunnelID":...})
  echo "$TUNNEL_CREDENTIALS" > /root/.cloudflared/credentials.json
  cloudflared tunnel --config /etc/cloudflared/config.yml run \
    > cloudflared.log 2>&1 &
  echo "[entry] cloudflared up (credentials)"
else
  echo "[entry] WARNING: no TUNNEL_TOKEN or TUNNEL_CREDENTIALS — tunnel NOT started"
fi

# Paywall + facilitator
env SELF_FACILITATE=1 NETWORK=eip155:8453 BIND_HOST=0.0.0.0 PAY_TO_SOL="${PAY_TO_SOL:-7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk}" \
  PUBLIC_BASE="${PUBLIC_BASE:-https://api.6766587364.lol}" \
  python api_server.py > api.log 2>&1 &
echo "[entry] api_server up"

# MCP bridge
python -m uvicorn mcp_bridge:mcp_app --host 127.0.0.1 --port 8010 --log-level warning > mcp_bridge.log 2>&1 &
echo "[entry] mcp_bridge up"

# Watchdog
bash fleet_watchdog.sh > watchdog.log 2>&1 &
echo "[entry] watchdog up"

# Keep alive; forward signals so the container stops cleanly
trap 'kill $(jobs -p) 2>/dev/null' TERM INT
wait
