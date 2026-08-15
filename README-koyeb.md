# Deploy to Koyeb (free, always-on — no phone needed)

Koyeb's free tier gives one always-on service (0.1 vCPU / 512MB) that does
**not** sleep. This runs the same api_server + MCP bridge + **the same
Cloudflare Tunnel** as the phone, so:

- URL stays `https://api.6766587364.lol` — no DNS or worker changes
- Phone + Koyeb both connected to the tunnel → Cloudflare routes to whichever
  is alive → **automatic failover, zero downtime**
- No ngrok anywhere in this stack

## Step 1 — Push to GitHub (you)

Create a repo (e.g. `MikeDorian375/echo-sentiment-koyeb`) and push the
`deploy/` folder contents. The Dockerfile + entrypoint + cloudflared config
live there. **Do NOT commit `state/`** — `.dockerignore` excludes it, and the
real keys go in as secrets.

## Step 2 — Get your tunnel token (you, one command)

Run this on the phone (proot Ubuntu), where cloudflared is installed:

```bash
cloudflared tunnel token d4c987e1-a617-46c7-ae5d-fd3cd9ebe4bb
```

Copy the long base64 string — that's your `TUNNEL_TOKEN`.

## Step 3 — Create the Koyeb app (you, ~5 min)

1. https://app.koyeb.com → sign up (GitHub login works).
2. **Create Service** → **GitHub** → pick the repo.
3. Builder: **Dockerfile** (auto-detected). Keep default amd64.
4. **Advanced → Environment variables** — add these (from this box's
   `state/` files; the first three are what make the paywall work):
   - `TUNNEL_TOKEN` = the token from Step 2
   - `GAS_KEY_EVM` = contents of `state/gas_key.txt`
   - `GAS_KEY_SOL` = contents of `state/sol_gas_key.txt`
   - `PAY_TO_SOL` = `7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk`
   - `PUBLIC_BASE` = `https://api.6766587364.lol` (optional — already the default)
5. Deploy. Wait for the build (~3-5 min).

> Alternative to `TUNNEL_TOKEN`: paste the full contents of
> `/root/.cloudflared/d4c987e1-a617-46c7-ae5d-fd3cd9ebe4bb.json` as
> `TUNNEL_CREDENTIALS` instead. Either works.

## Verify

```bash
curl -i https://api.6766587364.lol/v1/sample      # expect 200
curl -i https://api.6766587364.lol/v1/sentiment   # expect 402
```

If the phone is still connected, the tunnel answers from either side. To
prove Koyeb alone carries it: turn the phone's tunnel off (or airplane mode)
and re-run the curls — still 200/402.

## Failover notes

- **Two connectors, one tunnel**: Cloudflare's edge keeps connections to both
  the phone and the Koyeb container. If one drops, the other serves. No
  config needed — just run the tunnel in both places.
- **512MB is enough** (api ~250MB + cloudflared ~50MB + bridge ~80MB +
  watchdog).
- Koyeb's health check hits port 8000 `/` → returns 200 → satisfied.
- If the container restarts, the entrypoint re-materializes keys from env and
  cloudflared reconnects to the same tunnel. Watchdog covers crashes in
  between.
- `PUBLIC_BASE` is set inside the container so the OpenAPI + og:url advertise
  `https://api.6766587364.lol` (same as the phone).
