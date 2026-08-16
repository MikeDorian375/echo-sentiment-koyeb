#!/bin/bash
# Echo Sentiment — portable fleet launcher (bare-metal / no Docker).
# Same stack as the phone: Cloudflare Tunnel + API + MCP bridge +
# clawmerchants push + watchdog. One command, idempotent, self-verifying.
#
# Usage:  bash start_all.sh
# Logs:   api.log cloudflared.log mcp_bridge.log watchdog.log (in this dir)
#
# Secrets: reads state/tunnel_token.txt if TUNNEL_TOKEN is not exported.
#          Copy the state/ folder from the phone for full functionality.

cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
VENV="$DIR/.venv/bin"
URL="https://api.6766587364.lol"
PUBLIC_BASE="${PUBLIC_BASE:-https://api.6766587364.lol}"
PAY_TO_SOL="${PAY_TO_SOL:-7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk}"

say()  { echo -e "\n\033[1;36m[$1]\033[0m $2"; }
ok()   { echo -e "  \033[1;32m✓\033[0m $1"; }
fail() { echo -e "  \033[1;31m✗\033[0m $1"; }
warn() { echo -e "  \033[1;33m⚠\033[0m $1"; }

# Pick python: repo venv > system python3
if [ -x "$VENV/python" ]; then
  PY="$VENV/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
  VENV="$(dirname "$(dirname "$(command -v python3)")")"
else
  fail "no python3 found — install Python 3.11+ first"
  exit 1
fi

wait_port() { # wait_port <port> <timeout_s> <name>
  local i
  for i in $(seq 1 "$2"); do
    (echo > /dev/tcp/127.0.0.1/"$1") 2>/dev/null && return 0
    sleep 1
  done
  fail "$3 not listening on :$1 after ${2}s"
  return 1
}

# --- 0. cleanup stale processes (idempotent) ------------------------------
say "0/6" "Cleaning stale fleet processes"
pkill -f "crl_relay.py" 2>/dev/null; pkill -f "ngrok http" 2>/dev/null
pkill -f "api_server.py" 2>/dev/null; pkill -f "uvicorn mcp_bridge" 2>/dev/null
pkill -f "fleet_watchdog" 2>/dev/null; pkill -f "clawmerchants_push.py" 2>/dev/null
sleep 3
ok "stale processes cleared"

# --- 1. Cloudflare Tunnel (permanent static domain) -----------------------
say "1/6" "Cloudflare Tunnel → $URL"
TOKEN="${TUNNEL_TOKEN:-$(cat state/tunnel_token.txt 2>/dev/null)}"
CF_TUNNEL_ID="d4c987e1-a617-46c7-ae5d-fd3cd9ebe4bb"

# Local config path (repo cloudflared/config.yml), falling back to system
if [ -f "$DIR/cloudflared/config.yml" ]; then
  CF_CONFIG="$DIR/cloudflared/config.yml"
elif [ -f /root/.cloudflared/config.yml ]; then
  CF_CONFIG=/root/.cloudflared/config.yml
else
  CF_CONFIG="$DIR/cloudflared/config.yml"
fi

if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
  ok "cloudflared already running"
else
  if [ -n "$TOKEN" ]; then
    nohup cloudflared tunnel --config "$CF_CONFIG" run --token "$TOKEN" > cloudflared.log 2>&1 &
  else
    nohup cloudflared tunnel --config "$CF_CONFIG" run "$CF_TUNNEL_ID" > cloudflared.log 2>&1 &
  fi
  for i in $(seq 1 20); do
    curl -s -m 3 -o /dev/null "$URL/v1/sample" && break
    sleep 1
  done
  if curl -s -m 10 -o /dev/null "$URL/v1/sample"; then
    ok "tunnel serving through $URL"
  else
    fail "cloudflared not connected — see cloudflared.log"
  fi
fi

# --- 2. API server (paywall + facilitator) --------------------------------
say "2/6" "API server (paywall, dual-rail)"
if pgrep -f "api_server.py" >/dev/null 2>&1; then
  ok "api_server already running"
else
  nohup env SELF_FACILITATE=1 NETWORK=eip155:8453 PAY_TO_SOL="$PAY_TO_SOL" PUBLIC_BASE="$PUBLIC_BASE" \
    "$PY" api_server.py > api.log 2>&1 &
  wait_port 8000 30 "api_server" || true
  curl -s -m 5 -o /dev/null http://127.0.0.1:8000/ && ok "api_server up on :8000" || fail "api_server failed — see api.log"
fi

# --- 3. MCP bridge ---------------------------------------------------------
say "3/6" "MCP bridge"
if pgrep -f "uvicorn mcp_bridge" >/dev/null 2>&1; then
  ok "mcp_bridge already running"
else
  nohup "$PY" -m uvicorn mcp_bridge:mcp_app --host 127.0.0.1 --port 8010 --log-level warning > mcp_bridge.log 2>&1 &
  wait_port 8010 30 "mcp_bridge" || true
  curl -s -m 5 -o /dev/null http://127.0.0.1:8010/mcp && ok "mcp_bridge up on :8010" || fail "mcp_bridge failed"
fi

# --- 4. ClawMerchants push loop (needs state keys; skipped if missing) ----
say "4/6" "ClawMerchants data push loop"
if [ -f state/clawmerchants_key.txt ] && [ -f state/attestation_key.txt ]; then
  if pgrep -f "clawmerchants_push.py --loop" >/dev/null 2>&1; then
    ok "push loop already running"
  else
    nohup "$PY" clawmerchants_push.py --loop > clawmerchants_push.log 2>&1 &
    sleep 2
    pgrep -f "clawmerchants_push.py --loop" >/dev/null && ok "push loop running (15-min refresh)" || fail "push loop not running — see clawmerchants_push.log"
  fi
else
  warn "state keys missing — skipping clawmerchants (copy state/ from the phone to enable)"
fi

# --- 5. Watchdog + verification -------------------------------------------
say "5/6" "Watchdog + final verification"
if [ -f "$DIR/fleet_watchdog_local.sh" ]; then
  nohup bash "$DIR/fleet_watchdog_local.sh" > watchdog.log 2>&1 &
  ok "watchdog armed (revives any dead component within 30s)"
else
  fail "fleet_watchdog_local.sh not found"
fi

echo
sleep 8
echo "--- verification ---"
code=$(curl -s -m 20 -o /dev/null -w "%{http_code}" "$URL/v1/sentiment" -H "User-Agent: python-requests/2.32.0")
[ "$code" = "402" ] && ok "public paywall: HTTP 402 (correct — demanding payment)" || fail "public paywall: HTTP $code"
code=$(curl -s -m 20 -o /dev/null -w "%{http_code}" "$URL/v1/sample" -H "User-Agent: python-requests/2.32.0")
[ "$code" = "200" ] && ok "public free tier: HTTP 200" || fail "public free tier: HTTP $code"

echo
echo "Done. If any ✗ above: check the matching log file, or run: bash start_all.sh"
echo "Status anytime: $PY status.py"
