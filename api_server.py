"""x402 pay-per-call API — sells the sentiment engine as a micro-payment endpoint.

Protocol (x402 V2): a client GETs /v1/sentiment, gets HTTP 402 + a
PAYMENT-REQUIRED header (price, network, pay-to). The client pays USDC on Base
via the x402 facilitator, retries with a PAYMENT-SIGNATURE header, and the
server verifies + settles before returning the payload. No API keys, no
subscriptions — machine-native micropayments.

Products:
  GET /v1/sentiment  composite XLM market sentiment (-1..1) + live price/order book
  GET /v1/quote      XLM price + SDEX order book snapshot

Run (local test):
  .venv/bin/python api_server.py

Env (see config below):
  PAY_TO=0x...          Base wallet that receives payments (REQUIRED for real use)
  NETWORK=eip155:84532  Base Sepolia testnet (default) | eip155:8453 = Base mainnet
  PRICE_USD=0.005       USD per call

Notes:
  - Testnet (default) lets us exercise the full 402 handshake with zero funds.
  - Real usage needs PAY_TO set to a funded Base wallet + mainnet network.
"""
import os
import json as _json

import fastapi
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, Response, HTMLResponse
from datetime import datetime, timezone

import signals
import config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PAY_TO = os.environ.get("PAY_TO", "0x583FfEE3f6E0E8cAB3531fBd5C4e291784D3b6cD")  # J's Base wallet
PAY_TO_SOL = os.environ.get("PAY_TO_SOL", "")  # J's Solana address — enables the Solana rail
NETWORK = os.environ.get("NETWORK", "eip155:84532")   # Base Sepolia; 8453 = mainnet
PRICE_USD = os.environ.get("PRICE_USD", "0.005")      # $ per call
SOL_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")
# Solana-first promo: Solana rail priced at SOL_DISCOUNT_PCT of the Base price
# (default 0.6 = 40% off) to learn which chain converts better. Set SOL_PROMO=0 to disable.
SOL_PROMO = os.environ.get("SOL_PROMO", "1") == "1"
SOL_DISCOUNT_PCT = float(os.environ.get("SOL_DISCOUNT_PCT", "0.6"))
SAMPLE_LIMIT = int(os.environ.get("SAMPLE_LIMIT", "10"))  # free samples per IP per hour
# Enterprise volume deals: private endpoint, unlisted. $25 -> 10,000 credits
# = $0.0025/call effective (50% off base). Tune per-deal via env, or mint
# custom tokens with enterprise_mint.py for invoice-based deals.
ENTERPRISE_PRICE_USD = os.environ.get("ENTERPRISE_PRICE_USD", "25.00")
ENTERPRISE_CREDITS = int(os.environ.get("ENTERPRISE_CREDITS", "10000"))

import decimal as _decimal


def _accepts(price: str) -> list[dict]:
    """Payment options for a route: Base USDC always; Solana USDC when PAY_TO_SOL is set.

    During the Solana-first promo, the Solana rail is discounted to
    SOL_DISCOUNT_PCT of the Base price (e.g. $0.005 -> $0.003).
    """
    opts = [{"scheme": "exact", "payTo": PAY_TO, "price": price, "network": NETWORK}]
    if PAY_TO_SOL:
        sol_price = price
        if SOL_PROMO:
            p = _decimal.Decimal(price.lstrip("$")) * _decimal.Decimal(str(SOL_DISCOUNT_PCT))
            sol_price = "$" + format(p, "f").rstrip("0").rstrip(".")
        opts.append({"scheme": "exact", "payTo": PAY_TO_SOL, "price": sol_price,
                     "network": SOL_MAINNET_CAIP2})
    return opts

import threading
import time as _time
_sample_hits: dict = {}
_sample_lock = threading.Lock()

if PAY_TO in ("0x0000000000000000000000000000000000000001", ""):
    print("WARNING: PAY_TO not set — using placeholder receiver address. "
          "No real payments can settle. Set PAY_TO before going live.")

# ---------------------------------------------------------------------------
# x402 wiring (public facilitator by default; self-facilitate for mainnet)
# ---------------------------------------------------------------------------

from pathlib import Path

from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import register_exact_evm_server
from x402.extensions.bazaar import declare_discovery_extension, OutputConfig

# Bazaar discovery declarations — make /v1 endpoints indexable by
# bazaar-compatible facilitators (x402 ecosystem discovery layer).
_sentiment_bazaar = declare_discovery_extension(
    input={"method": "GET"},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "sentiment": 0.42,
            "sentiment_source": "FNG+FLOW+MOMENTUM",
            "components": {"fng": 0.3, "flow": 0.5, "momentum": 0.4},
            "price_usd": 0.0932,
            "mid": 0.0931,
            "spread_pct": 0.15,
            "ts": 1723000000.0,
        },
        schema={
            "type": "object",
            "properties": {
                "sentiment": {"type": "number"},
                "sentiment_source": {"type": "string"},
                "components": {"type": "object"},
                "price_usd": {"type": ["number", "null"]},
                "mid": {"type": ["number", "null"]},
                "spread_pct": {"type": ["number", "null"]},
                "ts": {"type": "number"},
            },
        },
    ),
)

_sentiment_bazaar["bazaar"]["info"]["input"]["method"] = "GET"
_quote_bazaar = declare_discovery_extension(
    input={"method": "GET"},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "price_usd": 0.0932,
            "best_bid": 0.0930,
            "best_ask": 0.0934,
            "spread_pct": 0.43,
        },
        schema={
            "type": "object",
            "properties": {
                "price_usd": {"type": "number"},
                "best_bid": {"type": "number"},
                "best_ask": {"type": "number"},
                "spread_pct": {"type": "number"},
            },
        },
    ),
)

_quote_bazaar["bazaar"]["info"]["input"]["method"] = "GET"
_arb_bazaar = declare_discovery_extension(
    input={"method": "GET"},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "rows": [
                {"leg": "SDEX -> Kraken", "buy": "SDEX @ 0.093000", "sell": "Kraken @ 0.093400", "net_pct": 0.21},
                {"leg": "Korea premium (info)", "buy": "SDEX @ 0.093000", "sell": "Upbit @ 0.094500 (KRW 130)", "net_pct": 1.61},
            ],
            "ts": 1723000000.0,
        },
        schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "leg": {"type": "string"},
                        "buy": {"type": "string"},
                        "sell": {"type": "string"},
                        "net_pct": {"type": "number"},
                    }},
                },
                "ts": {"type": "number"},
            },
        },
    ),
)

_arb_bazaar["bazaar"]["info"]["input"]["method"] = "GET"
_multi_bazaar = declare_discovery_extension(
    input={"method": "GET"},
    input_schema={
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET"]},
            "tickers": {"type": "array", "items": {"type": "string"}},
            "period": {"type": "string"},
        },
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "tickers": ["XLM-USD", "BTC-USD"],
            "period": "3mo",
            "cost_bps_per_side": 10,
            "rows": [
                {"asset": "XLM-USD", "strategy": "SMA crossover", "total_return_pct": 12.5, "sharpe": 1.2, "trades": 3, "vs_buy_hold_pct": 3.1},
            ],
            "ts": 1723000000.0,
        },
        schema={
            "type": "object",
            "properties": {
                "tickers": {"type": "array", "items": {"type": "string"}},
                "period": {"type": "string"},
                "cost_bps_per_side": {"type": "number"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "ts": {"type": "number"},
            },
        },
    ),
)

_multi_bazaar["bazaar"]["info"]["input"]["method"] = "GET"
SELF_FACILITATE = os.environ.get("SELF_FACILITATE", "0") == "1"
BASE_RPC = os.environ.get(
    "BASE_RPC",
    "https://mainnet.base.org" if NETWORK.endswith(":8453") else "https://base-sepolia.publicnode.com",
)

# Public base URL (Cloudflare Tunnel is live; ngrok retired).
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://api.6766587364.lol").rstrip("/")

app = FastAPI(title="Echo Sentiment API", version="0.1.0",
             servers=[{"url": PUBLIC_BASE,
                       "description": "Production (Base mainnet, USDC)"}])

# --- OpenAPI enrichment: embed x-payment-info per paid path (machine-discoverable
# pricing, same convention as other x402 v2 providers). The routes dict below is
# defined later, so we patch the schema lazily via custom_openapi().
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_original_openapi = app.openapi

def _custom_openapi():
    schema = _original_openapi()
    try:
        for key, cfg in routes.items():
            method, _, path = key.partition(" ")
            op = schema.get("paths", {}).get(path, {}).get(method.lower())
            if not op:
                continue
            offers = []
            base_price_usd = None
            for acc in cfg.get("accepts", []):
                net = acc.get("network", "")
                price_usd = acc.get("price", "$0.005").lstrip("$")
                if net.startswith("eip155:"):
                    if base_price_usd is None:
                        base_price_usd = price_usd
                    offers.append({
                        "intent": "charge", "method": "evm", "network": net,
                        "amount": str(int(round(float(price_usd) * 1e6))),
                        "currency": _USDC_BASE,
                        "description": f"{price_usd} USDC on Base ({net}), settled directly to the provider wallet via x402 exact",
                    })
                elif net.startswith("solana:"):
                    offers.append({
                        "intent": "charge", "method": "svm", "network": net,
                        "amount": str(int(round(float(price_usd) * 1e6))),
                        "currency": _USDC_SOL,
                        "description": f"{price_usd} USDC on Solana mainnet, settled directly to the provider wallet via x402 exact",
                    })
            op["x-payment-info"] = {
                "protocols": [{"x402": {}}],
                "price": {"mode": "fixed", "currency": "USD", "amount": f"{float(base_price_usd or '0.005'):.6f}"},
                "offers": offers,
            }
    except Exception:
        pass
    return schema

app.openapi = _custom_openapi


# MCP bridge lives in its own process (port 8010) — MCP servers need their own
# lifecycle (task group), which breaks when mounted as a sub-app. Proxy /mcp* here.
import httpx

_mcp_client = httpx.AsyncClient(base_url="http://127.0.0.1:8010", timeout=60)

@app.api_route("/mcp", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def mcp_proxy(request: Request, path: str = ""):
    target = f"/mcp" + (f"/{path}" if path else "")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    resp = await _mcp_client.request(request.method, target, content=body, headers=headers)
    resp_headers = {k: v for k, v in resp.headers.items()
                    if k.lower() not in ("content-length", "transfer-encoding")}
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)

if SELF_FACILITATE:
    # --- we ARE the facilitator (own thread/port, avoids self-deadlock) ---
    from facilitator_app import start_in_thread

    FACILITATOR_PORT = int(os.environ.get("FACILITATOR_PORT", "4022"))
    start_in_thread(FACILITATOR_PORT)
    print(f"[facilitator] self-facilitating on {NETWORK} | rpc: {BASE_RPC}")

    from x402.http import FacilitatorConfig

    server = x402ResourceServer(
        HTTPFacilitatorClient(FacilitatorConfig(url=f"http://127.0.0.1:{FACILITATOR_PORT}"))
    )
else:
    print(f"[facilitator] using public x402 facilitator ({NETWORK})")
    server = x402ResourceServer(HTTPFacilitatorClient())

register_exact_evm_server(server, networks=NETWORK)
from x402.mechanisms.svm.exact import register_exact_svm_server
register_exact_svm_server(server, networks=SOL_MAINNET_CAIP2, rpc_url=SOL_RPC)

_report_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "sentiment": 0.42,
            "verdict": "bullish",
            "confidence": "high",
            "detail": {"fng_index": 71, "order_flow_imbalance": 0.31, "momentum_24h_pct": 2.1},
            "price": {"usd": 0.0932, "spread_pct": 0.15},
            "arb_context": {"sdex_kraken_net_pct": -0.57, "korea_premium_pct": -0.22},
            "weights": {"fng": 0.3, "flow": 0.4, "momentum": 0.3},
        },
        schema={
            "type": "object",
            "properties": {
                "sentiment": {"type": "number"},
                "verdict": {"type": "string"},
                "confidence": {"type": "string"},
                "detail": {"type": "object"},
                "price": {"type": "object"},
                "arb_context": {"type": "object"},
                "weights": {"type": "object"},
            },
        },
    ),
)

_report_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

_bundle_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "credits": 10,
            "token": "bnd_1a2b3c4d",
            "expires": 1788700000,
            "usage": {"/v1/sentiment": 1, "/v1/sentiment-report": 10},
        },
        schema={
            "type": "object",
            "properties": {
                "credits": {"type": "integer"},
                "token": {"type": "string"},
                "expires": {"type": "number"},
                "usage": {"type": "object"},
            },
        },
    ),
)
_bundle_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

_history_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "series": [
                {"ts": 1786096800, "price_usd": 0.1617, "fng": -0.4, "momentum": 0.1, "sentiment": -0.15},
            ],
            "current": {"sentiment": -0.35, "sentiment_source": "FNG+FLOW+MOMENTUM", "flow": -0.71},
            "methodology": "FNG + price momentum reconstructed hourly; order-flow is live-only",
        },
        schema={
            "type": "object",
            "properties": {
                "series": {"type": "array", "items": {"type": "object"}},
                "current": {"type": "object"},
                "methodology": {"type": "string"},
            },
        },
    ),
)
_history_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

_fng_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={"index": 71, "label": "Greed", "mapped": 0.42, "ts": 1723000000},
        schema={
            "type": "object",
            "properties": {
                "index": {"type": "number"},
                "label": {"type": "string"},
                "mapped": {"type": "number"},
                "ts": {"type": "number"},
            },
        },
    ),
)
_fng_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

_network_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "network": "stellar",
            "ledger": {"sequence": 61234567, "successful_transactions": 312, "operations": 890},
            "supply_xlm": 50123456789.0,
            "xlm_usdc_trades_1h": {"trade_count": 1502, "xlm_volume": 81234.5, "vwap_usd": 0.1638},
        },
        schema={
            "type": "object",
            "properties": {
                "network": {"type": "string"},
                "ledger": {"type": "object"},
                "supply_xlm": {"type": "number"},
                "xlm_usdc_trades_1h": {"type": "object"},
            },
        },
    ),
)
_network_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

_attest_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "ticker": "xlm",
            "price_usd": 0.16377,
            "attestation": {"scheme": "ed25519-solana", "signer": "G...", "message": "{...}", "signature_b58": "..."},
        },
        schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "price_usd": {"type": "number"},
                "attestation": {"type": "object"},
            },
        },
    ),
)
_attest_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

_breadth_bazaar = declare_discovery_extension(
    input={},
    input_schema={
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["GET"]}},
        "required": ["method"],
    },
    output=OutputConfig(
        example={
            "top50": {"count": 50, "breadth_pct": 64.0, "avg_change_24h_pct": 1.2},
            "dominance": {"btc_pct": 54.1, "eth_pct": 8.7},
            "verdict": {"regime": "risk_on", "tone": "Broad participation with positive average move."},
            "xlm": {"rank_by_mcap": 18, "change_24h_pct": 2.4, "relative_strength": {"xlm_vs_btc_pp": 1.1, "xlm_vs_eth_pp": 1.9, "label": "outperforming"}},
        },
        schema={
            "type": "object",
            "properties": {
                "top50": {"type": "object"},
                "dominance": {"type": "object"},
                "verdict": {"type": "object"},
                "xlm": {"type": "object"},
            },
        },
    ),
)
_breadth_bazaar["bazaar"]["info"]["input"]["method"] = "GET"

SERVICE_NAME = "Echo Sentiment API"
ROUTE_MIME = "application/json"

routes = {
    "GET /v1/sentiment": {
        "accepts": _accepts(f"${PRICE_USD}"),
        "description": "Composite XLM market sentiment (-1..1) + live price",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "sentiment", "stellar"],
        "extensions": {"bazaar": _sentiment_bazaar["bazaar"]},
    },
    "GET /v1/quote": {
        "accepts": _accepts(f"${PRICE_USD}"),
        "description": "XLM price + SDEX order book snapshot",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "market-data", "stellar"],
        "extensions": {"bazaar": _quote_bazaar["bazaar"]},
    },
    "GET /v1/arb-opportunities": {
        "accepts": _accepts(f"${PRICE_USD}"),
        "description": "Live CEX<->SDEX arbitrage scan (net % after fees)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "arbitrage", "stellar", "market-data"],
        "extensions": {"bazaar": _arb_bazaar["bazaar"]},
    },
    "GET /v1/multi-asset": {
        "accepts": _accepts(f"${PRICE_USD}"),
        "description": "Strategy screen across major assets (return vs buy-and-hold)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "market-data", "strategies", "screen"],
        "extensions": {"bazaar": _multi_bazaar["bazaar"]},
    },
    "GET /v1/sentiment-report": {
        "accepts": _accepts("$0.05"),
        "description": "Premium: full XLM sentiment report (raw components, verdict, arb context)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "sentiment", "stellar", "premium", "report"],
        "extensions": {"bazaar": _report_bazaar["bazaar"]},
    },
    "GET /v1/bundle": {
        "accepts": _accepts("$0.05"),
        "description": "Prepaid bundle: $0.05 -> 10 credits (10 basic calls or 1 premium report)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "credits", "bundle", "bulk"],
        "extensions": {"bazaar": _bundle_bazaar["bazaar"]},
    },
    "GET /v1/sentiment-history": {
        "accepts": _accepts("$0.10"),
        "description": "24h hourly sentiment series (FNG + momentum reconstructed, live order-flow)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "sentiment", "stellar", "history", "trend"],
        "extensions": {"bazaar": _history_bazaar["bazaar"]},
    },
    "GET /v1/fng": {
        "accepts": _accepts("$0.01"),
        "description": "Cheap tick: Crypto Fear & Greed index (0-100) + label + mapped sentiment",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "sentiment", "fng", "tick"],
        "extensions": {"bazaar": _fng_bazaar["bazaar"]},
    },
    "GET /v1/xlm-network": {
        "accepts": _accepts("$0.01"),
        "description": "Stellar on-chain pulse: ledger health, supply, XLM/USDC SDEX trade stats (1h)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "stellar", "on-chain", "network", "pulse"],
        "extensions": {"bazaar": _network_bazaar["bazaar"]},
    },
    "GET /v1/attested-quote": {
        "accepts": _accepts("$0.01"),
        "description": "Oracle-grade quote: price + Ed25519 signature for verifiable agent data",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "oracle", "attestation", "verifiable", "market-data"],
        "extensions": {"bazaar": _attest_bazaar["bazaar"]},
    },
    "GET /v1/breadth": {
        "accepts": _accepts("$0.01"),
        "description": "Market regime: top-50 breadth %, avg 24h change, BTC dominance, risk-on/off verdict, XLM relative strength vs BTC/ETH",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "market-regime", "breadth", "macro", "verdict"],
        "extensions": {"bazaar": _breadth_bazaar["bazaar"]},
    },
    # Private volume tier — deliberately NOT in llms.txt/landing page.
    "GET /v1/bundle/enterprise": {
        "accepts": _accepts(f"${ENTERPRISE_PRICE_USD}"),
        "description": "Enterprise volume bundle (private deal)",
        "serviceName": SERVICE_NAME,
        "mimeType": ROUTE_MIME,
        "tags": ["crypto", "enterprise", "volume"],
    },
}

# --- bundle ledger (prepaid credits) ------------------------------------
# state/bundles.json: {token: {payer, credits, created, last_used}}
BUNDLE_CREDITS = int(os.environ.get("BUNDLE_CREDITS", "10"))
BUNDLE_PRICE_USD = "0.05"
PREMIUM_COST = 10  # credits for /v1/sentiment-report
CREDIT_COST = {  # credits per endpoint
    "/v1/sentiment": 1,
    "/v1/quote": 1,
    "/v1/arb-opportunities": 1,
    "/v1/multi-asset": 1,
    "/v1/sentiment-report": PREMIUM_COST,
    "/v1/sentiment-history": 20,  # $0.10 endpoint
    "/v1/fng": 2,  # $0.01 endpoint
    "/v1/xlm-network": 2,  # $0.01 endpoint
    "/v1/attested-quote": 2,  # $0.01 endpoint
    "/v1/breadth": 2,  # $0.01 endpoint
}

# --- XLM network pulse (Horizon on-chain stats) ---------------------------
_network_cache: dict = {"ts": 0.0, "data": None}


def _xlm_network() -> dict | None:
    """Stellar on-chain pulse from keyless Horizon: ledger health, supply, XLM/USDC trades."""
    now = _time.time()
    if now - _network_cache["ts"] < 60 and _network_cache["data"]:
        return _network_cache["data"]
    try:
        import requests as _req
        led = _req.get(f"{config.HORIZON}/ledgers?order=desc&limit=1", timeout=10).json()
        rec = led["_embedded"]["records"][0]
        _end = int(now * 1000)
        ta = _req.get(
            f"{config.HORIZON}/trade_aggregations",
            params={
                "base_asset_type": "native",
                "counter_asset_type": "credit_alphanum4",
                "counter_asset_code": config.QUOTE,
                "counter_asset_issuer": config.USDC_ISSUER,
                "resolution": 3600000,  # 1 hour
                "start_time": _end - 3 * 3600000,  # look back 3h for the latest populated bucket
                "end_time": _end,
                "order": "desc",
                "limit": 1,
            },
            timeout=10,
        ).json()
        recs = ta["_embedded"]["records"]
        trades = recs[0] if recs else None
        out = {
            "network": "stellar",
            "ledger": {
                "sequence": rec["sequence"],
                "closed_at": rec["closed_at"],
                "protocol_version": rec["protocol_version"],
                "successful_transactions": rec["successful_transaction_count"],
                "failed_transactions": rec["failed_transaction_count"],
                "operations": rec["operation_count"],
            },
            "supply_xlm": round(float(rec["total_coins"]), 2),
            "base_fee_xlm": rec["base_fee_in_stroops"] / 1e7,
            "base_reserve_xlm": rec["base_reserve_in_stroops"] / 1e7,
            "xlm_usdc_trades_1h": None if trades is None else {
                "trade_count": int(trades["trade_count"]),
                "xlm_volume": round(float(trades["base_volume"]), 2),
                "usdc_volume": round(float(trades["counter_volume"]), 2),
                "vwap_usd": round(float(trades["counter_volume"]) / float(trades["base_volume"]), 6) if float(trades["base_volume"]) else None,
                "close_usd": float(trades["close"]),
                "high_usd": float(trades["high"]),
                "low_usd": float(trades["low"]),
            },
            "note": "on-chain trade stats are XLM/USDC on Stellar SDEX, latest hourly bucket; prices in USD.",
            "ts": now,
        }
        _network_cache["data"], _network_cache["ts"] = out, now
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  [api] horizon pulse failed: {e}")
        return None


# --- signed price attestations (oracle-grade quotes) ----------------------
_ATTEST_KEY_PATH = Path(__file__).resolve().parent / "state" / "attestation_key.txt"
_attest_kp = None


def _load_attest_key():
    global _attest_kp
    try:
        from solders.keypair import Keypair
        if _ATTEST_KEY_PATH.exists():
            secret = bytes(_json.loads(_ATTEST_KEY_PATH.read_text()))
        else:
            _ATTEST_KEY_PATH.parent.mkdir(exist_ok=True)
            kp = Keypair()
            _ATTEST_KEY_PATH.write_text(_json.dumps(list(bytes(kp))))  # 64 ints, like sol_gas_key
            secret = bytes(kp)
        _attest_kp = Keypair.from_bytes(secret)
        print(f"[api] attestation signer: {_attest_kp.pubkey()}")
    except Exception as e:  # noqa: BLE001
        print(f"  [api] attestation key unavailable: {e}")


_TICKER_TO_CG = {
    "xlm": "stellar", "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "xrp": "ripple", "ada": "cardano", "link": "chainlink", "dot": "polkadot",
    "avax": "avalanche-2", "matic": "matic-network", "pol": "matic-network",
    "doge": "dogecoin", "uni": "uniswap", "aave": "aave", "arb": "arbitrum",
    "op": "optimism", "atom": "cosmos", "ltc": "litecoin", "near": "near",
}


def _attested_quote(ticker: str) -> dict:
    """Price for ticker + Ed25519 signature over a canonical message.

    Agents verify: decode `message` bytes, check ed25519 signature against the
    published signer pubkey (base58). Scheme: ed25519-solana (solders-compatible).
    """
    t = (ticker or "xlm").lower().strip()
    cg_id = _TICKER_TO_CG.get(t, t)
    import requests as _req
    r = _req.get(
        f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if cg_id not in data or "usd" not in data[cg_id]:
        raise ValueError(f"unknown ticker: {ticker}")
    price = round(float(data[cg_id]["usd"]), 6)
    ts = _time.time()
    msg = _json.dumps({"ticker": t, "price_usd": price, "ts": round(ts, 3)},
                      sort_keys=True, separators=(",", ":"))
    sig = _attest_kp.sign_message(msg.encode())
    return {
        "product": "xlm-attested-quote",
        "ticker": t,
        "coin_gecko_id": cg_id,
        "price_usd": price,
        "ts": ts,
        "attestation": {
            "scheme": "ed25519-solana",
            "signer": str(_attest_kp.pubkey()),
            "message": msg,
            "signature_b58": str(sig),
        },
        "verify": "ed25519 verify over the exact bytes of attestation.message "
                  "(utf-8) with attestation.signer pubkey.",
    }


# --- market regime / breadth (top-50, keyless CoinGecko) ------------------
_breadth_cache: dict = {"ts": 0.0, "data": None}


def _breadth() -> dict | None:
    """Top-50 market regime: breadth %, avg 24h change, BTC dominance, risk
    verdict, XLM relative strength vs BTC/ETH. Interpreted, rules-based."""
    now = _time.time()
    if now - _breadth_cache["ts"] < 60 and _breadth_cache["data"]:
        return _breadth_cache["data"]
    try:
        import requests as _req
        m = _req.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 50,
                "page": 1,
                "price_change_percentage": "24h",
                "sparkline": "false",
            },
            timeout=12,
        )
        m.raise_for_status()
        coins = m.json()
        if not coins:
            raise ValueError("empty top-50 response")
        g = _req.get("https://api.coingecko.com/api/v3/global", timeout=12)
        g.raise_for_status()
        gd = g.json()["data"]
        mcap_pct = gd.get("market_cap_percentage", {})
        btc_dom = round(float(mcap_pct.get("btc", 0.0)), 2)
        eth_dom = round(float(mcap_pct.get("eth", 0.0)), 2)

        changes = {}
        for c in coins:
            ch = c.get("price_change_percentage_24h")
            if ch is not None:
                changes[c["symbol"].lower()] = ch
        vals = list(changes.values())
        n = len(vals)
        if not n:
            raise ValueError("no 24h changes in top-50")
        up = sum(1 for v in vals if v > 0)
        breadth_pct = round(100.0 * up / n, 1)
        avg_change = round(sum(vals) / n, 2)
        btc_ch, eth_ch, xlm_ch = changes.get("btc"), changes.get("eth"), changes.get("xlm")
        xlm_rank = next((i + 1 for i, c in enumerate(coins) if c["symbol"].lower() == "xlm"), None)

        # risk verdict: rules on breadth + average move
        if breadth_pct >= 60 and avg_change >= 0.5:
            verdict, tone = "risk_on", "Broad participation with a positive average move — risk appetite is strong."
        elif breadth_pct <= 40 and avg_change <= -0.5:
            verdict, tone = "risk_off", "Broad decline — risk appetite is off; treat bounces with suspicion."
        elif breadth_pct >= 55 and avg_change > 0:
            verdict, tone = "mild_risk_on", "Slightly positive breadth — cautiously constructive."
        elif breadth_pct <= 45 and avg_change < 0:
            verdict, tone = "mild_risk_off", "Slightly negative breadth — cautiously defensive."
        else:
            verdict, tone = "mixed", "No clear regime — breadth and average move disagree."

        # XLM relative strength vs majors
        rs = {"label": "mixed"}
        if xlm_ch is not None:
            if btc_ch is not None:
                rs["xlm_vs_btc_pp"] = round(xlm_ch - btc_ch, 2)
            if eth_ch is not None:
                rs["xlm_vs_eth_pp"] = round(xlm_ch - eth_ch, 2)
            vs_btc, vs_eth = rs.get("xlm_vs_btc_pp", 0), rs.get("xlm_vs_eth_pp", 0)
            if vs_btc > 0 and vs_eth > 0:
                rs["label"] = "outperforming"
            elif vs_btc < 0 and vs_eth < 0:
                rs["label"] = "underperforming"

        out = {
            "product": "market-regime-breadth",
            "top50": {"count": n, "breadth_pct": breadth_pct, "avg_change_24h_pct": avg_change},
            "dominance": {"btc_pct": btc_dom, "eth_pct": eth_dom,
                          "note": "global crypto market-cap share (CoinGecko /global)"},
            "verdict": {"regime": verdict, "tone": tone},
            "xlm": {"rank_by_mcap": xlm_rank, "change_24h_pct": xlm_ch, "relative_strength": rs},
            "source": "CoinGecko (keyless, top-50 by market cap, 24h window)",
            "ts": now,
        }
        _breadth_cache["data"], _breadth_cache["ts"] = out, now
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  [api] breadth failed: {e}")
        return None


_bundle_path = Path(__file__).resolve().parent / "state" / "bundles.json"
_bundles: dict = {}
_bundle_lock = threading.Lock()


def _load_bundles():
    global _bundles
    try:
        if _bundle_path.exists():
            _bundles = _json.loads(_bundle_path.read_text())
            print(f"[bundles] loaded {len(_bundles)} tokens from {_bundle_path}")
        else:
            print(f"[bundles] no ledger file at {_bundle_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[bundles] LOAD ERROR: {e}")
        _bundles = {}


def _save_bundles():
    try:
        _bundle_path.parent.mkdir(exist_ok=True)
        _bundle_path.write_text(_json.dumps(_bundles))
    except Exception as e:  # noqa: BLE001
        print(f"[bundles] SAVE ERROR: {e}")


def _consume_bundle(token: str, path: str) -> bool:
    """Deduct one credit for path; returns True if the token is valid + has credits."""
    cost = CREDIT_COST.get(path)
    if cost is None:
        return False
    with _bundle_lock:
        b = _bundles.get(token)
        if not b or b.get("credits", 0) < cost:
            return False
        b["credits"] -= cost
        b["last_used"] = _time.time()
        _save_bundles()
        return True


_load_bundles()
_load_attest_key()

# ---------------------------------------------------------------------------
# V1 -> V2 payment header shim (interop with MetaMask mcp-x402 and other V1 clients)
#
# MetaMask's official mcp-x402 server emits V1 payment headers:
#   {x402Version: 1, scheme, network: "base", payload: {signature, authorization}}
# while this server (x402 Python SDK) is V2-only on the server side and the
# SDK crashes (AttributeError) if a V1 payload reaches find_matching_requirements.
#
# The EIP-3009 signature covers ONLY the authorization fields + EIP-712 domain
# (name/version/chainId/verifyingContract = asset). The wire envelope
# (x402Version, network string, field names) is NOT signed, so remapping it is
# cryptographically safe: the same signature verifies against the V2 payload.
#
# We convert inbound V1 headers to V2 BEFORE the SDK sees them:
#   - legacy network name -> CAIP-2 (base -> eip155:8453)
#   - amount/payTo taken from the signed authorization (what the facilitator
#     validates against requirements anyway)
#   - asset + EIP-712 domain extra taken from the network config the server
#     itself advertises, so accepted matches the advertised requirement exactly
# ---------------------------------------------------------------------------

# V1 legacy network name -> CAIP-2 (from x402/mechanisms/evm/v1/constants.py)
_V1_LEGACY_TO_CAIP2 = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "ethereum": "eip155:1",
    "polygon": "eip155:137",
    "polygon-amoy": "eip155:80002",
    "avalanche": "eip155:43114",
    "avalanche-fuji": "eip155:43113",
    "abstract": "eip155:2741",
    "abstract-testnet": "eip155:11124",
    "iotex": "eip155:4689",
    "sei": "eip155:1329",
    "sei-testnet": "eip155:713715",
    "peaq": "eip155:3338",
    "story": "eip155:1513",
    "educhain": "eip155:656476",
    "skale-base-sepolia": "eip155:1444673419",
    "megaeth": "eip155:4326",
    "monad": "eip155:143",
    "stable": "eip155:988",
    "stable-testnet": "eip155:2201",
    "celo": "eip155:42220",
    "flare": "eip155:14",
}


def _v1_to_v2_header(raw_b64: str):
    """Convert a V1 x402 payment header to a V2 header. Returns None if not V1."""
    try:
        import base64
        data = _json.loads(base64.b64decode(raw_b64))
    except Exception:
        return None
    if data.get("x402Version") != 1:
        return None
    legacy_net = data.get("network")
    caip2 = _V1_LEGACY_TO_CAIP2.get(legacy_net)
    if not caip2:
        # already CAIP-2 (e.g. client-side adapter pre-converted it) -> pass through
        import re as _re
        if isinstance(legacy_net, str) and _re.match(r"^[a-z0-9]+:[0-9]+$", legacy_net):
            caip2 = legacy_net
    if not caip2:
        return None
    scheme = data.get("scheme")
    payload = data.get("payload") or {}
    auth = payload.get("authorization") or {}
    # asset + EIP-712 domain from the network config we advertise
    asset = ""
    extra = {}
    try:
        from x402.mechanisms.evm.utils import get_network_config
        cfg = get_network_config(caip2)
        da = cfg.get("default_asset") or {}
        asset = da.get("address", "")
        if da.get("name"):
            extra = {"name": da["name"], "version": da.get("version", "")}
    except Exception:
        pass
    v2 = {
        "x402Version": 2,
        "accepted": {
            "scheme": scheme,
            "network": caip2,
            "asset": asset,
            "amount": str(auth.get("value", "")),
            "pay_to": auth.get("to", ""),
            "max_timeout_seconds": 300,
            "extra": extra,
        },
        "payload": payload,
    }
    import base64
    return base64.b64encode(_json.dumps(v2).encode()).decode()


@app.middleware("http")
async def x402_payment_gate(request, call_next):
    # Bundle bypass: a valid prepaid token serves the call without a new payment.
    token = request.headers.get("x-bundle-token")
    if token and _consume_bundle(token, request.url.path):
        return await call_next(request)
    # V1 shim: rewrite a V1 payment header to V2 before the SDK middleware sees it.
    raw = request.headers.get("payment-signature") or request.headers.get("x-payment")
    if raw:
        v2 = _v1_to_v2_header(raw)
        if v2:
            # Re-inject as the V2 header under the name the SDK reads.
            scope = dict(request.scope)
            headers = []
            replaced = False
            for k, v in scope["headers"]:
                if k.lower() in (b"payment-signature", b"x-payment"):
                    if not replaced:
                        headers.append((b"payment-signature", v2.encode()))
                        replaced = True
                else:
                    headers.append((k, v))
            if not replaced:
                headers.append((b"payment-signature", v2.encode()))
            scope["headers"] = headers
            request = Request(scope)
    return await payment_middleware(routes, server)(request, call_next)


@app.middleware("http")
async def agent_discovery_headers(request, call_next):
    """RFC 8288 Link headers for agent discovery (llmstxt.org convention).

    Lets AI agents discover llms.txt / OpenAPI / x402 payment metadata from
    response headers alone (no HTML parsing). Applied to every response.
    """
    base = str(request.base_url).rstrip("/")
    response = await call_next(request)
    response.headers.append("Link", f'<{base}/llms.txt>; rel="ai-manifest"')
    response.headers.append("Link", f'<{base}/openapi.json>; rel="service-desc"; type="application/vnd.oai.openapi+json"')
    response.headers.append("Link", f'<{base}/.well-known/x402>; rel="payment-method"')
    response.headers.append("Link", f'<{base}/robots.txt>; rel="describedby"')
    return response


@app.middleware("http")
async def receipt_middleware(request, call_next):
    """Stamp paid 200 responses with provenance: payload hash + payer.

    The x402 payment-response header (from settlement) already carries the
    tx hash; this adds a body-hash receipt so paid results are verifiable
    and replayable. Bundle-token calls have no per-call payment, so they
    carry no receipt (the bundle purchase itself was the paid event).
    """
    import hashlib
    response = await call_next(request)
    pp = getattr(request.state, "payment_payload", None)
    if pp is None or response.status_code != 200:
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    h = hashlib.sha256(body).hexdigest()
    payer = None
    try:
        d = pp.model_dump(by_alias=True) if hasattr(pp, "model_dump") else pp
        payer = d.get("payload", {}).get("authorization", {}).get("from")
    except Exception:
        pass
    headers = dict(response.headers)
    headers["x-receipt-hash"] = h
    if payer:
        headers["x-receipt-payer"] = payer
    return Response(content=body, status_code=200, headers=headers,
                    media_type=response.media_type)


# ---------------------------------------------------------------------------
# Free (unpaid) endpoints
# ---------------------------------------------------------------------------

@app.get("/spec.pdf")
def spec_pdf():
    """Serve the OpenAPI spec PDF (for marketplace reviewers)."""
    return FileResponse("echo-sentiment-openapi.pdf", media_type="application/pdf")


@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    """XML sitemap for AI agents and search crawlers (robots.txt -> sitemap.xml)."""
    base = str(request.base_url).rstrip("/")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Public/landing URLs only — paid endpoints are discovered via /llms.txt + /openapi.json.
    urls = [
        "/",
        "/llms.txt",
        "/robots.txt",
        "/openapi.json",
        "/spec.pdf",
        "/.well-known/x402",
        "/v1/sample",
    ]
    items = "".join(
        f"  <url><loc>{base}{u}</loc><lastmod>{now}</lastmod><changefreq>daily</changefreq></url>\n"
        for u in urls
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{items}</urlset>
"""
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt")
def robots_txt(request: Request):
    """AI content-signals policy (llmstxt.org convention) — machine-readable permission grants."""
    base = str(request.base_url).rstrip("/")
    return Response(content=f"""User-agent: *
Allow: /

# Content signals (https://llmstxt.org/robots.txt convention)
# search  = building a search index / search results
# ai-input = inputting content into AI models (RAG, grounding, generative answers)
# ai-train = training or fine-tuning AI models
#
# Echo Sentiment API: API documentation and endpoint descriptions may be used
# for search and AI input (agents need /llms.txt and /openapi.json to discover
# and pay for the API). Training/fine-tuning is not granted.
Content-Signal: search=yes
Content-Signal: ai-input=yes
Content-Signal: ai-train=no

Sitemap: {base}/sitemap.xml
""", media_type="text/plain")


@app.get("/llms.txt")
def llms_txt():
    """Machine-readable service summary for AI agents (llmstxt.org convention)."""
    return Response(content=f"""# Echo Sentiment API

Real-time XLM (Stellar) market intelligence, pay-per-call via x402 (HTTP 402, USDC on Base mainnet). No API keys, no subscriptions — the payment is the authentication.

## Endpoints

- /v1/sample — FREE (10/hour/IP) — reduced sentiment snapshot: score, source, live price. Taste before you pay.
- /v1/sentiment — $0.005 — composite XLM market sentiment in [-1, 1] (Crypto Fear & Greed + SDEX order-flow imbalance + 24h momentum), live price, spread.
- /v1/quote — $0.005 — XLM price, best bid/ask, spread, book depth.
- /v1/arb-opportunities — $0.005 — live CEX<->SDEX arbitrage scan (Kraken, Upbit, SDEX), net % after fees.
- /v1/multi-asset — $0.005 — strategy backtest screen (SMA crossover, RSI mean-reversion, Buy&Hold) across 8 major assets; params: tickers (comma-separated), period (e.g. 3mo, 1y).
- /v1/sentiment-report — $0.05 — premium full report: verdict (bullish/bearish/neutral), confidence, raw FNG index + label, order-flow imbalance, book depth, 24h momentum %, arb context.
- /v1/bundle — $0.05 — prepaid bundle: 10 credits (10 basic calls or 1 premium report). Send header X-Bundle-Token: <token> on any paid endpoint to spend credits instead of paying per call.
- /v1/sentiment-history — $0.10 — hourly sentiment series (default 24h, ?hours= up to 168) — FNG + momentum reconstructed, live order-flow in 'current', trend direction.
- /v1/fng — $0.01 — cheap decision tick: Fear & Greed index (0-100), label, mapped sentiment.
- /v1/xlm-network — $0.01 — Stellar on-chain pulse: latest ledger (sequence, tx/op counts), XLM supply, base fee/reserve, and XLM/USDC SDEX trade stats for the latest hourly bucket (count, volumes, VWAP, OHLC).
- /v1/attested-quote — $0.01 — oracle-grade quote: price for a ticker (default xlm; btc/eth/sol/xrp/ada/link/… supported) + Ed25519 signature so agents can independently verify the data. See Attestations below.
- /v1/breadth — $0.01 — market regime: top-50 breadth % (share of top-50 up in 24h), average 24h change, BTC/ETH dominance, rules-based risk-on/off/mixed verdict, XLM rank by market cap + relative strength vs BTC and ETH (pp). Interpreted signal, not raw tables.

## Discovery

- /robots.txt — AI content-signals policy (search/ai-input granted, ai-train denied)
- /openapi.json — OpenAPI spec with per-path x-payment-info (price + dual-chain offers embedded, machine-readable)
- /.well-known/x402 — machine-readable manifest (name, payTo, price, endpoints)

## Attestations (verifiable quotes)

Every /v1/attested-quote response includes an `attestation` object:
- scheme: ed25519-solana
- signer: 4xtjYkLTS2DCNhShNitRxhozYpgA4S3am4ut961PS4cN (our oracle pubkey)
- message: canonical JSON string (sorted keys, compact separators)
- signature_b58: base58 Ed25519 signature over the exact UTF-8 bytes of `message`

Verify with solders (or any ed25519 lib):
    from solders.signature import Signature
    from solders.pubkey import Pubkey
    sig = Signature.from_string(resp["attestation"]["signature_b58"])
    pub = Pubkey.from_string(resp["attestation"]["signer"])
    valid = sig.verify(pub, resp["attestation"]["message"].encode())  # -> True/False

## Payment flow (x402 v2)

1. GET any paid endpoint -> HTTP 402 + PAYMENT-REQUIRED header (base64 JSON with accepts[]).
2. Sign an EIP-3009 permit for the accepted amount (5000 or 50000 microUSDC) to 0x583FfEE3f6E0E8cAB3531fBd5C4e291784D3b6cD.
3. Retry with PAYMENT-SIGNATURE header -> HTTP 200 + data.

Network: eip155:8453 (Base) + solana mainnet. Asset: USDC. Facilitator: self-hosted (no third party).

## Solana-first promo

The Solana rail is temporarily discounted 40% (0.6x Base price) — e.g. /v1/sentiment is $0.003 on Solana vs $0.005 on Base. Promo may end without notice; check the 402 accepts[] for live prices.

## Receipts (provenance)

Every paid 200 response carries two receipt headers:
- payment-response: settlement proof from the facilitator (success, payer, on-chain transaction hash, network)
- x-receipt-hash: SHA-256 of the response body, so results are verifiable and replayable
- x-receipt-payer: the address that paid for the call
""", media_type="text/plain")


@app.get("/")
def info(request: Request = None):
    data = {
        "name": "Echo Sentiment API",
        "payment": "x402 (HTTP 402, USDC on Base)",
        "endpoints": {
            "/v1/sentiment": f"${PRICE_USD}/call — composite XLM sentiment",
            "/v1/quote": f"${PRICE_USD}/call — XLM price + order book",
            "/v1/arb-opportunities": f"${PRICE_USD}/call — CEX<->SDEX arb scan",
            "/v1/multi-asset": f"${PRICE_USD}/call — strategy screen, 8 assets",
            "/v1/fng": "$0.01/call — Fear & Greed index tick",
            "/v1/xlm-network": "$0.01/call — Stellar on-chain pulse",
            "/v1/attested-quote": "$0.01/call — oracle-grade signed price",
            "/v1/breadth": "$0.01/call — market regime: top-50 breadth, BTC dominance, risk verdict, XLM relative strength",
            "/v1/sentiment-report": "$0.05/call — premium full report",
            "/v1/bundle": "$0.05 → 10 credits (prepaid)",
            "/v1/sentiment-history": "$0.10 — 24h sentiment trend series",
            "/v1/sample": "FREE — limited sentiment snapshot (rate-limited)",
        },
        "status": "testnet" if "84532" in NETWORK else "mainnet",
        "facilitator": "self-hosted" if SELF_FACILITATE else "public x402.org",
    }
    # Browsers get an HTML landing page with OpenGraph; agents keep JSON.
    accept = (request.headers.get("accept") or "") if request else ""
    if "text/html" in accept:
        rails = "Base + Solana" if PAY_TO_SOL else "Base"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Echo Sentiment API — XLM market intelligence for AI agents</title>
<meta name="description" content="Real-time XLM market sentiment, price, arbitrage and strategy data. Pay-per-call in USDC via x402 — no API keys, no subscriptions.">
<meta property="og:title" content="Echo Sentiment API — XLM market intelligence for AI agents">
<meta property="og:description" content="Real-time XLM market sentiment, price, arbitrage and strategy data. Pay-per-call in USDC via x402 — no API keys, no subscriptions.">
<meta property="og:type" content="website">
<meta property="og:url" content="{PUBLIC_BASE}/">
<meta name="twitter:card" content="summary">
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 760px; margin: 0 auto; padding: 24px 20px 60px; color: #1a1a1a; line-height: 1.55; }}
code {{ background: #f2f2f2; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e5e5e5; }}
th {{ background: #f8f8f8; }}
.price {{ white-space: nowrap; font-weight: 600; }}
.badge {{ display: inline-block; background: #e8f5e9; color: #1b5e20; border-radius: 12px; padding: 2px 10px; font-size: 0.8em; }}
.rails {{ display: inline-block; background: #e3f2fd; color: #0d47a1; border-radius: 12px; padding: 2px 10px; font-size: 0.8em; }}
</style>
</head>
<body>
<h1>🌀 Echo Sentiment API</h1>
<p>Real-time <strong>XLM (Stellar)</strong> market intelligence for AI agents. Composite sentiment from the Crypto Fear &amp; Greed Index, SDEX order-flow imbalance, and 24h price momentum — <strong>rules only, no LLM in the loop</strong>.</p>
<p><span class="badge">Live on {NETWORK.split(":")[1] if ":" in NETWORK else NETWORK}</span> <span class="rails">{rails}</span> <span class="badge">x402 v2</span> <span class="badge" style="background:#fff3e0;color:#e65100">Solana promo: 40% off</span></p>
<p>Pay per call in <strong>USDC</strong> via <a href="https://x402.org">x402</a> — HTTP 402 → sign an EIP-3009 permit (or Solana transfer) → 200. <strong>No account, no API key, no subscription.</strong> The payment is the authentication.</p>
<h2>Paid endpoints</h2>
<table>
<tr><th>Endpoint</th><th>Price</th><th>What you get</th></tr>
<tr><td><code>/v1/sentiment</code></td><td class="price">$0.005</td><td>Composite XLM sentiment (−1..1), components, live price, spread</td></tr>
<tr><td><code>/v1/quote</code></td><td class="price">$0.005</td><td>XLM price, best bid/ask, spread, book depth</td></tr>
<tr><td><code>/v1/arb-opportunities</code></td><td class="price">$0.005</td><td>CEX↔SDEX arbitrage scan (Kraken, Upbit), net % after fees</td></tr>
<tr><td><code>/v1/multi-asset</code></td><td class="price">$0.005</td><td>Strategy screen across 8 assets (SMA/RSI/Bollinger vs Buy&Hold)</td></tr>
<tr><td><code>/v1/fng</code></td><td class="price">$0.01</td><td>Crypto Fear &amp; Greed index + label (cheap decision tick)</td></tr>
<tr><td><code>/v1/xlm-network</code></td><td class="price">$0.01</td><td>Stellar on-chain pulse — ledger, supply, SDEX trade stats</td></tr>
<tr><td><code>/v1/attested-quote</code></td><td class="price">$0.01</td><td>Oracle-grade price + Ed25519 signature (verifiable)</td></tr>
<tr><td><code>/v1/breadth</code></td><td class="price">$0.01</td><td>Market regime — top-50 breadth %, BTC dominance, risk-on/off verdict, XLM vs BTC/ETH</td></tr>
<tr><td><code>/v1/sentiment-report</code></td><td class="price">$0.05</td><td>Premium: verdict, confidence, raw components, arb context</td></tr>
<tr><td><code>/v1/bundle</code></td><td class="price">$0.05</td><td>10 prepaid credits — bulk discount, one payment</td></tr>
<tr><td><code>/v1/sentiment-history</code></td><td class="price">$0.10</td><td>24h hourly sentiment series + trend direction</td></tr>
</table>
<h2>Try it free</h2>
<p><code>GET /v1/sample</code> — free, rate-limited: <a href="/v1/sample">{PUBLIC_BASE}/v1/sample</a></p>
<h2>Payment flow</h2>
<ol>
<li>Agent requests any paid endpoint → server replies <strong>HTTP 402</strong> + <code>payment-required</code> header (amount, asset, network, payTo).</li>
<li>Agent signs the payment (EIP-3009 permit on Base, or SPL transfer on Solana) via its x402 facilitator.</li>
<li>Agent retries with <code>payment-signature</code> → server verifies on-chain → <strong>200 + data</strong>.</li>
</ol>
<h2>For agents</h2>
<p><a href="/llms.txt">/llms.txt</a> · <a href="/openapi.json">OpenAPI</a> · <a href="/.well-known/x402">manifest</a> · MCP server: <code>/mcp</code> (tool <code>get_xlm_sentiment</code>)</p>
<h2>Builders</h2>
<p>Open source MCP server: <a href="https://github.com/MikeDorian375/echo-sentiment-mcp">github.com/MikeDorian375/echo-sentiment-mcp</a> (MIT). Pay with bundle credits via <code>X-Bundle-Token</code> header.</p>
</body>
</html>"""
        return HTMLResponse(html)
    return data


@app.get("/.well-known/x402")
def manifest():
    """Discovery stub for x402-aware clients."""
    manifest = {
        "name": "Echo Sentiment API",
        "payment_scheme": "exact",
        "network": NETWORK,
        "pay_to": PAY_TO,
        "price_usd": float(PRICE_USD),
        "endpoints": ["/v1/sentiment", "/v1/quote", "/v1/arb-opportunities",
                       "/v1/multi-asset", "/v1/sentiment-report"],
    }
    if PAY_TO_SOL:
        manifest["solana"] = {"network": SOL_MAINNET_CAIP2, "pay_to": PAY_TO_SOL}
    return JSONResponse(manifest)


# ---------------------------------------------------------------------------
# Paid endpoints (behind the x402 gate)
# ---------------------------------------------------------------------------

@app.get("/v1/sentiment")
def sentiment():
    sig = signals.collect_signal()
    return {
        "product": "xlm-market-sentiment",
        "sentiment": round(sig["sentiment"], 4),
        "sentiment_source": sig["sentiment_source"],
        "components": sig["sentiment_components"],
        "price_usd": sig["price_usd"],
        "mid": sig["mid"],
        "spread_pct": round(sig["spread"] * 100, 3) if sig.get("spread") else None,
        "ts": sig["ts"],
    }


@app.get("/v1/quote")
def quote():
    book = signals.get_order_book()
    price = signals.get_xlm_price_usd()
    return {
        "product": "xlm-quote",
        "price_usd": price,
        "mid": book.get("mid"),
        "bid": book.get("bid"),
        "ask": book.get("ask"),
        "spread_pct": round(book["spread"] * 100, 3) if book.get("spread") else None,
        "bid_depth": book.get("bid_depth"),
        "ask_depth": book.get("ask_depth"),
        "ts": book.get("ts"),
    }


@app.get("/v1/arb-opportunities")
def arb_opportunities():
    import arb
    return {"product": "arb-opportunities", **arb.scan()}


@app.get("/v1/multi-asset")
def multi_asset(tickers: str = "XLM-USD,BTC-USD,ETH-USD,SOL-USD,ADA-USD,LINK-USD,AVAX-USD,DOGE-USD",
                period: str = "3mo"):
    import lab
    tlist = [t.strip() for t in tickers.split(",") if t.strip()]
    return {"product": "multi-asset-screen", **lab.screen_json(tlist, period)}


@app.get("/v1/sentiment-report")
def sentiment_report():
    """Premium $0.05 — full report: raw components, verdict, arb context."""
    import arb
    book = signals.get_order_book()

    fng_val, fng_label = signals.get_fear_greed()
    flow_val, _ = signals.get_order_flow(book)
    mom_val, mom_label = signals.get_momentum()
    score, source, components = signals.get_sentiment_score(book)

    live_count = sum(v is not None for v in (fng_val, flow_val, mom_val))
    confidence = {3: "high", 2: "medium", 1: "low"}.get(live_count, "none")
    if source.startswith("MOCK"):
        confidence = "none"

    if score >= 0.25:
        verdict = "bullish"
    elif score <= -0.25:
        verdict = "bearish"
    else:
        verdict = "neutral"

    # FNG raw index (0-100) + label
    fng_index = round(fng_val * 50 + 50, 1) if fng_val is not None else None
    fng_label_txt = None
    if fng_index is not None:
        if fng_index < 25: fng_label_txt = "extreme fear"
        elif fng_index < 45: fng_label_txt = "fear"
        elif fng_index < 55: fng_label_txt = "neutral"
        elif fng_index < 75: fng_label_txt = "greed"
        else: fng_label_txt = "extreme greed"

    price = signals.get_xlm_price_usd()
    spread = round(book["spread"] * 100, 3) if book.get("spread") else None

    arb_ctx = {}
    try:
        for row in arb.scan().get("rows", []):
            if row["leg"] == "SDEX -> Kraken":
                arb_ctx["sdex_kraken_net_pct"] = round(row["net_pct"], 3)
            elif row["leg"] == "Korea premium (info)":
                arb_ctx["korea_premium_pct"] = round(row["net_pct"], 3)
    except Exception:
        pass

    return {
        "product": "xlm-sentiment-report",
        "sentiment": round(score, 4),
        "verdict": verdict,
        "confidence": confidence,
        "sentiment_source": source,
        "components": components,
        "detail": {
            "fng_index": fng_index,
            "fng_label": fng_label_txt,
            "order_flow_imbalance": round(flow_val, 4) if flow_val is not None else None,
            "bid_depth": book.get("bid_depth"),
            "ask_depth": book.get("ask_depth"),
            "momentum_24h_pct": float(mom_label.strip("mom()").replace("%", "")) if mom_label.startswith("mom(") and "down" not in mom_label else None,
        },
        "price": {"usd": price, "mid": book.get("mid"), "spread_pct": spread},
        "arb_context": arb_ctx,
        "weights": {
            "fng": config.SENTIMENT_WEIGHT_FNG,
            "flow": config.SENTIMENT_WEIGHT_FLOW,
            "momentum": config.SENTIMENT_WEIGHT_MOMENTUM,
        },
        "ts": book.get("ts"),
    }


@app.get("/v1/sample")
def sample(request: Request):
    """FREE taste of the data — rate-limited, no payment required.

    Simple in-memory fixed-window limiter per IP (SAMPLE_LIMIT per hour).
    Returns a reduced sentiment snapshot to hook agents into the paid tiers.
    """
    ip = request.client.host if request.client else "unknown"
    now = _time.time()
    with _sample_lock:
        window = _sample_hits.setdefault(ip, [])
        window = [t for t in window if now - t < 3600]
        if len(window) >= SAMPLE_LIMIT:
            _sample_hits[ip] = window
            return JSONResponse({"error": "rate limit", "detail": f"{SAMPLE_LIMIT}/hour free"},
                                status_code=429)
        window.append(now)
        _sample_hits[ip] = window

    sig = signals.collect_signal()
    return {
        "product": "xlm-sentiment-sample",
        "sentiment": round(sig["sentiment"], 4),
        "sentiment_source": sig["sentiment_source"],
        "price_usd": sig["price_usd"],
        "upgrade": "$0.005 — GET /v1/sentiment (full: components, mid, spread) | $0.05 — /v1/sentiment-report",
        "ts": sig["ts"],
    }


@app.get("/v1/bundles/debug")
def bundles_debug():
    """Debug: ledger path + contents + file state (internal)."""
    return {
        "ledger_path": str(_bundle_path),
        "file_exists": _bundle_path.exists(),
        "file_mtime": _bundle_path.stat().st_mtime if _bundle_path.exists() else None,
        "in_memory": {t[:14]: b["credits"] for t, b in list(_bundles.items())[-5:]},
        "in_memory_count": len(_bundles),
    }


@app.get("/v1/bundle")
@app.get("/v1/bundle/enterprise")
def bundle_purchase(request: Request):
    """Paid: standard bundle $0.05 -> 10 credits; enterprise $25 -> 10,000 credits.

    Issues a bundle token after payment verifies. Enterprise tier is private
    (unlisted) volume pricing for agent operators.
    """
    is_enterprise = request.url.path.endswith("/enterprise")
    credits = ENTERPRISE_CREDITS if is_enterprise else BUNDLE_CREDITS
    price = ENTERPRISE_PRICE_USD if is_enterprise else BUNDLE_PRICE_USD

    # The x402 middleware stores the verified payment payload on request.state.
    payer = None
    pp = getattr(request.state, "payment_payload", None)
    if pp is not None:
        try:
            if hasattr(pp, "model_dump"):
                d = pp.model_dump(by_alias=True)
            elif isinstance(pp, dict):
                d = pp
            else:
                d = {}
            payer = d.get("payload", {}).get("authorization", {}).get("from") or \
                    d.get("accepted", {}).get("payTo") or "unknown"
        except Exception:
            payer = None

    token = "bnd_" + os.urandom(16).hex()
    with _bundle_lock:
        _bundles[token] = {
            "payer": payer,
            "credits": credits,
            "created": _time.time(),
            "last_used": None,
        }
        _save_bundles()
    return {
        "product": "xlm-credit-bundle-enterprise" if is_enterprise else "xlm-credit-bundle",
        "credits": credits,
        "price_usd": price,
        "effective_per_call_usd": round(float(price) / credits, 6) if credits else None,
        "token": token,
        "payer": payer,
        "expires": _time.time() + 30 * 86400,
        "usage": {"/v1/sentiment": 1, "/v1/quote": 1, "/v1/arb-opportunities": 1,
                  "/v1/multi-asset": 1, "/v1/sentiment-report": PREMIUM_COST,
                  "/v1/sentiment-history": 20, "/v1/fng": 2},
        "how_to_use": "Send header X-Bundle-Token: <token> on any paid endpoint.",
    }


@app.get("/v1/fng")
def fng_tick():
    """$0.01 — cheap decision tick: Fear & Greed index."""
    val, label = signals.get_fear_greed()
    if val is None:
        return JSONResponse({"error": "FNG source down"}, status_code=503)
    index = round(val * 50 + 50, 1)
    if index < 25:
        lbl = "extreme fear"
    elif index < 45:
        lbl = "fear"
    elif index < 55:
        lbl = "neutral"
    elif index < 75:
        lbl = "greed"
    else:
        lbl = "extreme greed"
    return {
        "product": "fng-tick",
        "index": index,
        "label": lbl,
        "mapped": round(val, 4),
        "source": label,
        "ts": _time.time(),
    }


@app.get("/v1/xlm-network")
def xlm_network():
    """$0.01 — Stellar on-chain pulse (keyless Horizon, cached 60s)."""
    data = _xlm_network()
    if data is None:
        return JSONResponse({"error": "Horizon source down"}, status_code=503)
    return data


@app.get("/v1/attested-quote")
def attested_quote(ticker: str = "xlm"):
    """$0.01 — oracle-grade quote: price + Ed25519 attestation."""
    if _attest_kp is None:
        return JSONResponse({"error": "attestation key unavailable"}, status_code=503)
    try:
        return _attested_quote(ticker)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"source failed: {e}"}, status_code=502)


@app.get("/v1/breadth")
def breadth():
    """$0.01 — market regime / breadth: top-50 breadth %, avg change, BTC
    dominance, risk-on/off verdict, XLM relative strength vs BTC/ETH."""
    data = _breadth()
    if data is None:
        return JSONResponse({"error": "CoinGecko source down"}, status_code=503)
    return data


@app.get("/v1/sentiment-history")
def sentiment_history(hours: int = 24):
    """$0.10 — hourly sentiment series (default 24h, up to 168h).

    Reconstructs the composite from hourly price history (momentum component) +
    Fear & Greed (daily index). Order-flow imbalance is live-only, so it's
    included as the current bar rather than backfilled.
    """
    hours = max(1, min(int(hours), 168))
    import requests as _requests
    days = max(2, hours // 24 + 2)  # enough history for the momentum lookback
    try:
        r = _requests.get(
            f"https://api.coingecko.com/api/v3/coins/stellar/market_chart?vs_currency=usd&days={days}&interval=hourly",
            timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        raw = r.json().get("prices", [])
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "price history unavailable", "detail": str(e)[:120]}, status_code=503)

    fng_val, _ = signals.get_fear_greed()
    flow_val, _ = signals.get_order_flow(signals.get_order_book())

    w_fng = config.SENTIMENT_WEIGHT_FNG
    w_flow = config.SENTIMENT_WEIGHT_FLOW
    w_mom = config.SENTIMENT_WEIGHT_MOMENTUM

    series = []
    for i in range(24, len(raw)):  # 24h lookback for momentum
        ts_ms, price = raw[i]
        prev = raw[i - 24][1]
        chg = (price - prev) / prev * 100 if prev else 0.0
        mom = max(-1.0, min(1.0, chg / 5.0))
        fng = fng_val if fng_val is not None else 0.0
        # historical bars: FNG + momentum (order-flow not reconstructable)
        w_sum = w_fng + w_mom
        sent = (w_fng * fng + w_mom * mom) / w_sum if w_sum else 0.0
        series.append({
            "ts": ts_ms / 1000,
            "price_usd": round(price, 6),
            "fng": round(fng, 3),
            "momentum": round(mom, 3),
            "sentiment": round(sent, 4),
        })
    series = series[-hours:]

    # current bar: full composite incl. live order-flow
    current = {"sentiment": None, "sentiment_source": None, "flow": None}
    try:
        book = signals.get_order_book()
        score, source, _comps = signals.get_sentiment_score(book)
        current = {"sentiment": round(score, 4), "sentiment_source": source, "flow": flow_val}
    except Exception:
        pass

    return {
        "product": "xlm-sentiment-history",
        "hours": len(series),
        "series": series,
        "current": current,
        "methodology": ("Hourly bars: FNG (daily) + 24h price momentum, weighted "
                         f"{w_fng}/{w_mom} over their sum. Order-flow imbalance is "
                         "live-only and appears in 'current'."),
    }


if __name__ == "__main__":
    import uvicorn

    print(f"[api] Echo Sentiment API — {NETWORK} — ${PRICE_USD}/call")
    print("[api] test: curl -i http://127.0.0.1:8000/v1/sentiment  (expect 402)")
    # BIND_HOST: default loopback (ngrok tunnels to it locally); set 0.0.0.0 on
    # Koyeb so the load balancer can reach the container (no ngrok there).
    _bind = os.environ.get("BIND_HOST", "127.0.0.1")
    uvicorn.run(app, host=_bind, port=8000, log_level="warning")
