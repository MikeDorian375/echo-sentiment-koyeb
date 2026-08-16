#!/usr/bin/env python3
"""clawmerchants_push.py — push fresh Echo Sentiment data to ClawMerchants assets.

Fetches live data from the same sources the API uses (Horizon + CoinGecko),
signs an Ed25519 attestation with the production attestation key, and pushes
payloads to our ClawMerchants assets via PUT /v1/provider-data/:assetId.

Usage:
  ./.venv/bin/python clawmerchants_push.py          # one push cycle
  ./.venv/bin/python clawmerchants_push.py --loop   # push every 15 min forever
"""
import json
import sys
import time
from pathlib import Path

import requests as _req

BASE = "https://clawmerchants.com"
KEY_PATH = Path(__file__).resolve().parent / "state" / "clawmerchants_key.txt"
ATTEST_KEY_PATH = Path(__file__).resolve().parent / "state" / "attestation_key.txt"

# asset ids: name -> id (created 2026-08-08)
ASSETS = {
    "xlm-network": "b4ca1220-2f80-4e5a-b827-7633f990c009",   # XLM On-Chain Network Pulse
    "attested-quote": "0aa9cc41-d3fe-4ce1-9f69-91a6895785ba",  # Attested XLM Quote
    "mcp": "ce1cae92-3b52-47c4-83c6-08f45e7413b2",            # Echo Sentiment MCP Server
    "breadth": "a16bd510-390d-40bb-ae95-ec8c0e8b4c3a",         # Market Regime — Top-50 Breadth & Risk Verdict
}

HORIZON = "https://horizon.stellar.org"
USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"

_TICKER_TO_CG = {
    "xlm": "stellar", "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "xrp": "ripple", "ada": "cardano", "link": "chainlink", "dot": "polkadot",
    "avax": "avalanche-2", "pol": "matic-network", "doge": "dogecoin",
    "uni": "uniswap", "aave": "aave", "arb": "arbitrum", "op": "optimism",
    "atom": "cosmos", "ltc": "litecoin", "near": "near",
}


def _load_attest_kp():
    from solders.keypair import Keypair
    secret = bytes(json.loads(ATTEST_KEY_PATH.read_text()))
    return Keypair.from_bytes(secret)


def attested_quote(kp, ticker="xlm") -> dict:
    cg_id = _TICKER_TO_CG.get(ticker, ticker)
    r = _req.get(
        f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    price = round(float(data[cg_id]["usd"]), 6)
    ts = time.time()
    msg = json.dumps({"ticker": ticker, "price_usd": price, "ts": round(ts, 3)},
                     sort_keys=True, separators=(",", ":"))
    sig = kp.sign_message(msg.encode())
    return {
        "product": "xlm-attested-quote",
        "ticker": ticker,
        "coin_gecko_id": cg_id,
        "price_usd": price,
        "ts": ts,
        "attestation": {
            "scheme": "ed25519-solana",
            "signer": str(kp.pubkey()),
            "message": msg,
            "signature_b58": str(sig),
        },
        "verify": "ed25519 verify over the exact bytes of attestation.message "
                  "(utf-8) with attestation.signer pubkey.",
    }


def xlm_network() -> dict | None:
    """Same payload as /v1/xlm-network (keyless Horizon)."""
    now = time.time()
    try:
        led = _req.get(f"{HORIZON}/ledgers?order=desc&limit=1", timeout=10).json()
        rec = led["_embedded"]["records"][0]
        _end = int(now * 1000)
        ta = _req.get(
            f"{HORIZON}/trade_aggregations",
            params={
                "base_asset_type": "native",
                "counter_asset_type": "credit_alphanum4",
                "counter_asset_code": "USDC",
                "counter_asset_issuer": USDC_ISSUER,
                "resolution": 3600000,
                "start_time": _end - 3 * 3600000,
                "end_time": _end,
                "order": "desc",
                "limit": 1,
            },
            timeout=10,
        ).json()
        recs = ta["_embedded"]["records"]
        trades = recs[0] if recs else None
        return {
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
    except Exception as e:  # noqa: BLE001
        print(f"  [push] horizon pulse failed: {e}")
        return None


def breadth() -> dict | None:
    """Same payload as /v1/breadth (keyless CoinGecko, 24h top-50 regime)."""
    try:
        m = _req.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd", "order": "market_cap_desc", "per_page": 50,
                "page": 1, "price_change_percentage": "24h", "sparkline": "false",
            },
            timeout=12,
        )
        m.raise_for_status()
        coins = m.json()
        if not coins:
            raise ValueError("empty top-50 response")
        g = _req.get("https://api.coingecko.com/api/v3/global", timeout=12)
        g.raise_for_status()
        mcap_pct = g.json()["data"].get("market_cap_percentage", {})
        changes = {}
        for c in coins:
            ch = c.get("price_change_percentage_24h")
            if ch is not None:
                changes[c["symbol"].lower()] = ch
        vals = list(changes.values())
        n = len(vals)
        up = sum(1 for v in vals if v > 0)
        breadth_pct = round(100.0 * up / n, 1)
        avg_change = round(sum(vals) / n, 2)
        btc_ch, eth_ch, xlm_ch = changes.get("btc"), changes.get("eth"), changes.get("xlm")
        xlm_rank = next((i + 1 for i, c in enumerate(coins) if c["symbol"].lower() == "xlm"), None)
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
        return {
            "product": "market-regime-breadth",
            "top50": {"count": n, "breadth_pct": breadth_pct, "avg_change_24h_pct": avg_change},
            "dominance": {"btc_pct": round(float(mcap_pct.get("btc", 0.0)), 2),
                          "eth_pct": round(float(mcap_pct.get("eth", 0.0)), 2),
                          "note": "global crypto market-cap share (CoinGecko /global)"},
            "verdict": {"regime": verdict, "tone": tone},
            "xlm": {"rank_by_mcap": xlm_rank, "change_24h_pct": xlm_ch, "relative_strength": rs},
            "source": "CoinGecko (keyless, top-50 by market cap, 24h window)",
            "ts": time.time(),
        }
    except Exception as e:  # noqa: BLE001
        print(f"  [push] breadth failed: {e}")
        return None


MCP_PAYLOAD = {
    "product": "echo-sentiment-mcp",
    "description": "MCP server with XLM market intelligence tools for MCP-capable agents.",
    "tools": ["sentiment", "quote", "xlm_network", "attested_quote", "fng"],
    "endpoint": "https://api.6766587364.lol/mcp",
    "transport": "streamable-http (MCP)",
    "install": "uvx --from git+https://github.com/MikeDorian375/echo-sentiment-mcp mcp-sentiment",
    "payment": "x402 — HTTP 402 on Base (USDC) or Solana (USDC); no API key required.",
    "docs": "https://api.6766587364.lol/llms.txt",
    "free_tier": "https://api.6766587364.lol/v1/sample",
}


def push(asset_key: str, data: dict) -> bool:
    key = KEY_PATH.read_text().strip()
    r = _req.put(
        f"{BASE}/v1/provider-data/{ASSETS[asset_key]}",
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        json={"data": data},
        timeout=15,
    )
    ok = r.status_code == 200
    print(f"  [push] {asset_key}: HTTP {r.status_code} {'OK' if ok else r.text[:200]}")
    return ok


def cycle() -> bool:
    kp = _load_attest_kp()
    ok = True
    q = attested_quote(kp)
    ok &= push("attested-quote", q)
    n = xlm_network()
    if n is not None:
        ok &= push("xlm-network", n)
    else:
        print("  [push] skipping xlm-network (source failed)")
    b = breadth()
    if b is not None:
        ok &= push("breadth", b)
    else:
        print("  [push] skipping breadth (source failed)")
    ok &= push("mcp", MCP_PAYLOAD)
    return ok


if __name__ == "__main__":
    if "--loop" in sys.argv:
        print("[push] loop mode — every 900s")
        while True:
            try:
                cycle()
            except Exception as e:  # noqa: BLE001
                print(f"  [push] cycle error: {e}")
            time.sleep(900)
    else:
        sys.exit(0 if cycle() else 1)
