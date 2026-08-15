# Echo Sentiment API — deploy kit

This folder is the deployable package for running the Echo Sentiment API
stack on an always-on host (Koyeb free tier), using the **same Cloudflare
Tunnel** as the primary box for automatic failover.

## Contents

- `Dockerfile` — python:3.13-slim + cloudflared + api_server + mcp_bridge
- `entrypoint.sh` — materializes secrets, starts tunnel + api + bridge + watchdog
- `fleet_watchdog.sh` — restarts dead components inside the container
- `cloudflared/config.yml` — tunnel ingress (api.6766587364.lol → :8000)
- `api_server.py`, `mcp_bridge.py` + support modules
- `requirements.txt` — pinned deps
- `README-koyeb.md` — the 5-minute setup guide

## Quick start

Follow `README-koyeb.md`: push to GitHub → create Koyeb app → add 4 secrets
(`TUNNEL_TOKEN`, `GAS_KEY_EVM`, `GAS_KEY_SOL`, `PAY_TO_SOL`) → deploy.

## Local test (optional)

```bash
docker build -t echo-koyeb .
docker run -e TUNNEL_TOKEN=... -e GAS_KEY_EVM=... -e GAS_KEY_SOL=... \
  -e PAY_TO_SOL=7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk -p 8000:8000 echo-koyeb
```
