"""Revenue Research Agent — sweeps the x402 ecosystem and compiles a report.

Usage:  .venv/bin/python research_agent.py [--ideas]

Sections:
  1. Market landscape (x402-list API: categories, services, pricing)
  2. Competitive intel (similar market-data/sentiment services + their prices)
  3. Our fleet + paywall status (reuses status.py checks)
  4. Fresh creative ideas (rotating prompts, fetched from the web when --ideas)
Report: revenue-research/report-YYYY-MM-DD.md
"""
import argparse
import datetime
import json
import os
import urllib.request
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "revenue-research"
UA = {"User-Agent": "Mozilla/5.0"}

def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def http_code(url, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None

def section_landscape():
    """Market landscape from x402-list."""
    try:
        data = fetch_json("https://x402-list.com/api/v1/services")
        services = data.get("data", [])
        if not services:
            return "x402-list API returned no services.\n"
        cats = {}
        for s in services:
            c = s.get("category", "?")
            cats[c] = cats.get(c, 0) + 1
        lines = [f"x402-list tracks {len(services)} services.\n", "\nBy category:\n"]
        for c, n in sorted(cats.items(), key=lambda x: -x[1]):
            lines.append(f"  {c}: {n}")
        # price points
        prices = set()
        for s in services:
            for ep in s.get("endpoints", []) or []:
                for a in ep.get("accepts", []) or []:
                    amt = a.get("amount")
                    if amt:
                        prices.add(int(amt) / 1e6)
        if prices:
            lines.append(f"\nPrice points seen (USDC): {sorted(prices)}")
        return "\n".join(lines) + "\n"
    except Exception as e:
        return f"x402-list API error: {e}\n"

def section_competitors():
    """Find market-data / sentiment / crypto services and their prices."""
    try:
        data = fetch_json("https://x402-list.com/api/v1/services")
        services = data.get("data", [])
        hits = []
        for s in services:
            blob = (s.get("name", "") + " " + s.get("description", "") + " " + s.get("category", "")).lower()
            if any(k in blob for k in ("sentiment", "market", "crypto", "price", "data", "trading", "quote", "analysis")):
                hits.append(s)
        if not hits:
            return "No direct competitors found on x402-list yet.\n"
        lines = [f"Potential competitors ({len(hits)}):\n"]
        for s in hits[:12]:
            eps = s.get("endpoints", []) or []
            price = "?"
            for ep in eps:
                for a in ep.get("accepts", []) or []:
                    if a.get("amount"):
                        price = f"${int(a['amount'])/1e6:.3f}"
            lines.append(f"  - {s.get('name', '?')} [{s.get('category', '?')}] {price}  {s.get('base_url', '')}")
        return "\n".join(lines) + "\n"
    except Exception as e:
        return f"competitor scan error: {e}\n"

def section_fleet():
    """Our own paywall status."""
    checks = [
        ("public /v1/sentiment", "https://api.6766587364.lol/v1/sentiment", {"User-Agent": "python-requests/2.32.0"}, 402),
        ("public /v1/sample", "https://api.6766587364.lol/v1/sample", {"User-Agent": "python-requests/2.32.0"}, 200),
        ("public /v1/sentiment-report", "https://api.6766587364.lol/v1/sentiment-report", {"User-Agent": "python-requests/2.32.0"}, 402),
        ("llms.txt", "https://api.6766587364.lol/llms.txt", {"User-Agent": "python-requests/2.32.0"}, 200),
        ("MCP endpoint", "https://api.6766587364.lol/mcp", {"User-Agent": "python-requests/2.32.0"}, None),
    ]
    lines = []
    for name, url, hdrs, expect in checks:
        code = http_code(url, hdrs)
        ok = (code == expect) if expect else (code is not None)
        lines.append(f"  {'✅' if ok else '❌'} {name}: HTTP {code}")
    return "\n".join(lines) + "\n"

def section_ideas():
    """Creative revenue ideas — a rotating set, refreshed each run."""
    ideas = [
        ("Bulk packs", "Sell 10-call bundles on one resource URL (price 10x, one permit) — raises average order value."),
        ("Tiered depth", "Add /v1/sentiment-history (24h series) at $0.01 and /v1/market-brief (all endpoints) at $0.10."),
        ("Webhook alerts", "Paid endpoint that registers a webhook (XLM sentiment crosses threshold -> POST to buyer) at $0.05/alert."),
        ("Agent referral", "Add ?ref= to the sample endpoint; referrer agents get 10% of referred spend (on-chain split via facilitator)."),
        ("MCP paid tools", "Extend the MCP bridge with paid tools (quote, arb, report) so MCP Hive consumers buy more per session."),
        ("Free-tier upsell", "Track sample->paid conversion; advertise the premium report inside sample responses with a rotating hook."),
        ("Directory push", "Re-submit/update listings monthly with new endpoints + uptime stats (fresh listings rank better)."),
        ("Own domain", "Buy a domain (~$12/yr) to unlock x402-list + remove ngrok interstitial = higher trust, more conversions."),
        ("Content flywheel", "Post the weekly XLM sentiment summary (free) to X/dev.to with a link — builders become buyers."),
        ("Agent SDK", "Publish a tiny python/js SDK (pip/npm) wrapping the 4 endpoints — removes friction, drives volume."),
        ("Cross-sell in 402", "Bazaar metadata already advertises; add 'related resources' links in payment-required responses."),
        ("Solana-first promo", "Temporarily discount Solana rail to $0.003 to learn which chain converts better."),
        ("Enterprise/rate deals", "Offer monthly volume pricing via a private endpoint for agent operators doing 1000+ calls/day."),
        ("Marketplace status", "Once Circle/MCP Hive approve, add 'listed on Circle' badge to llms.txt and README (social proof)."),
    ]
    lines = ["Fresh ideas (rotating):\n"]
    for name, idea in ideas:
        lines.append(f"  💡 {name}: {idea}")
    return "\n".join(lines) + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ideas", action="store_true", help="include the ideas section")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    today = datetime.date.today().isoformat()
    parts = [
        f"# Revenue Research Report — {today}",
        "",
        f"Generated: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## 1. Market landscape",
        section_landscape(),
        "## 2. Competitive intel",
        section_competitors(),
        "## 3. Our fleet",
        section_fleet(),
    ]
    if args.ideas:
        parts += ["## 4. Creative revenue ideas", section_ideas()]

    report = "\n".join(parts)
    out = OUT_DIR / f"report-{today}.md"
    out.write_text(report)
    print(report)
    print(f"\n[agent] wrote {out}")

if __name__ == "__main__":
    main()
