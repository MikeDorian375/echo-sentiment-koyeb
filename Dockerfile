# Echo Sentiment API — Koyeb always-on deploy (Cloudflare Tunnel)
# Pin versions to exactly what's verified working in the sandbox venv.
FROM python:3.13-slim

WORKDIR /app

# cloudflared (amd64 — Koyeb default arch) + curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && cloudflared --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api_server.py facilitator_app.py mcp_bridge.py signals.py config.py \
     green_path.py solana_green_path.py arb.py lab.py main.py strategy.py broker.py portfolio.py ./
COPY entrypoint.sh fleet_watchdog.sh ./
COPY cloudflared/config.yml /etc/cloudflared/config.yml
RUN chmod +x entrypoint.sh fleet_watchdog.sh

ENV SELF_FACILITATE=1 \
    NETWORK=eip155:8453 \
    BIND_HOST=0.0.0.0 \
    PUBLIC_BASE=https://api.6766587364.lol \
    PYTHONUNBUFFERED=1

EXPOSE 8000 8010

CMD ["./entrypoint.sh"]
