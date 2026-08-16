# Echo Sentiment API — deploy kit

This folder is the deployable package for running the Echo Sentiment API
stack on any host (Docker **or** bare-metal), using the **same Cloudflare
Tunnel** as the primary box for automatic failover.

## Contents

- `Dockerfile` — python:3.13-slim + cloudflared + api_server + mcp_bridge
- `entrypoint.sh` — materializes secrets, starts tunnel + api + bridge + watchdog (container)
- `fleet_watchdog.sh` — restarts dead components inside the container
- `start_all.sh` — portable one-command launcher for bare-metal (no Docker)
- `fleet_watchdog_local.sh` — watchdog for bare-metal (started by start_all.sh)
- `status.py` — status dashboard (wallets + fleet + discovery checks)
- `clawmerchants_push.py` — marketplace data push loop (needs state/ keys)
- `cloudflared/config.yml` — tunnel ingress (api.6766587364.lol → :8000)
- `api_server.py`, `mcp_bridge.py` + support modules
- `requirements.txt` — pinned deps
- `README-koyeb.md` — the 5-minute Docker/Koyeb setup guide

## Quick start — Docker (Koyeb / any Docker host)

Follow `README-koyeb.md`: push to GitHub → create Koyeb app → add 4 secrets
(`TUNNEL_TOKEN`, `GAS_KEY_EVM`, `GAS_KEY_SOL`, `PAY_TO_SOL`) → deploy.

## Quick start — bare-metal (no Docker)

```bash
git clone https://github.com/MikeDorian375/echo-sentiment-koyeb
cd echo-sentiment-koyeb
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# install cloudflared for your arch:
#   amd64: curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
#   arm64: curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /usr/local/bin/cloudflared
#   chmod +x /usr/local/bin/cloudflared
# copy secrets from the primary box (see below)
bash start_all.sh
```

### Secrets

Copy these files from the primary box's `xlm-agent/state/` folder into the
repo's `state/` folder (never commit them — `.gitignore` excludes `state/`):

| File | Needed for |
|---|---|
| `tunnel_token.txt` | Cloudflare Tunnel (same tunnel → same URL, auto-failover) |
| `gas_key.txt` | Base gas wallet (EVM rail) |
| `sol_gas_key.txt` | Solana gas keypair (Solana rail) |
| `clawmerchants_key.txt` + `attestation_key.txt` | clawmerchants push loop (optional) |

Alternatively export as env vars (`TUNNEL_TOKEN`, `GAS_KEY_EVM`, `GAS_KEY_SOL`,
`PAY_TO_SOL`) — `start_all.sh` reads `TUNNEL_TOKEN` from env first.

## Why this works as a transition artifact

- Same tunnel ID (`d4c987e1-…`) → the new host joins the same tunnel as the
  phone. Cloudflare load-balances across both connectors — **automatic
  failover** if either box goes down.
- `start_all.sh` is idempotent: safe to re-run after a reboot.
- Watchdog revives any dead component within 30s.
- `status.py` gives you the same dashboard as the primary box.
