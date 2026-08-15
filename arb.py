"""CEX <-> SDEX arbitrage scanner for XLM.

The classic low-capital, short-horizon strategy: buy where XLM is cheap,
sell where it's expensive, net of costs. This module SCANS real prices from
multiple venues and reports net-of-fee opportunities. No orders, no risk.

Venues (all public, keyless):
  - SDEX  (Horizon order book, XLM/USDC)
  - Kraken (XLM/USD ticker)
  - Upbit  (KRW-XLM ticker — reported as informational "Korea premium",
            since KRW legs have KYC/FX friction in practice)

Usage:
  python3 arb.py --once     # single scan (default)
  python3 arb.py --watch    # loop every WATCH_SECONDS
"""
import argparse
import time

import requests

import config
import signals

KRAKEN_URL = "https://api.kraken.com/0/public/Ticker?pair=XLMUSD"
UPBIT_URL = "https://api.upbit.com/v1/ticker?markets=KRW-XLM"
FX_URL = "https://open.er-api.com/v6/latest/USD"  # free, keyless FX rates


# ---------------------------------------------------------------------------
# Venue feeds
# ---------------------------------------------------------------------------

def get_kraken() -> dict:
    """Kraken XLM/USD best bid/ask."""
    try:
        r = requests.get(KRAKEN_URL, timeout=10)
        r.raise_for_status()
        d = r.json()["result"]["XXLMZUSD"]
        return {"bid": float(d["b"][0]), "ask": float(d["a"][0])}
    except Exception as e:  # noqa: BLE001
        print(f"  [arb] Kraken failed: {e}")
        return {"bid": None, "ask": None}


def get_upbit_usd() -> dict:
    """Upbit XLM/KRW trade price converted to USD (informational)."""
    try:
        r = requests.get(UPBIT_URL, timeout=10)
        r.raise_for_status()
        krw = float(r.json()[0]["trade_price"])
        fx = requests.get(FX_URL, timeout=10).json()["rates"]["KRW"]
        return {"price_usd": krw / fx, "price_krw": krw, "usdkrw": fx}
    except Exception as e:  # noqa: BLE001
        print(f"  [arb] Upbit/FX failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# Opportunity math
# ---------------------------------------------------------------------------

def net_pct(buy_price: float, sell_price: float, buy_fee_bps: float,
            sell_fee_bps: float) -> float:
    """Net profit % after fees on both legs (slippage already in prices)."""
    gross = (sell_price - buy_price) / buy_price * 100
    return gross - (buy_fee_bps + sell_fee_bps) / 100


def scan() -> dict:
    print("  [arb] scanning venues...")
    sdex = signals.get_order_book()          # XLM/USDC on SDEX
    kraken = get_kraken()                    # XLM/USD on Kraken
    upbit = get_upbit_usd()                  # XLM/USD equivalent on Upbit

    rows = []
    sdex_bid, sdex_ask = sdex.get("bid"), sdex.get("ask")
    k_bid, k_ask = kraken.get("bid"), kraken.get("ask")

    if sdex_ask and k_bid:
        # Direction A: buy on SDEX (pay ask + slippage), sell on Kraken (bid - Kraken fee)
        eff_sdex_ask = sdex_ask * (1 + config.SLIPPAGE_BPS / 10000)
        rows.append({
            "leg": "SDEX -> Kraken",
            "buy": f"SDEX @ {eff_sdex_ask:.6f}",
            "sell": f"Kraken @ {k_bid:.6f}",
            "net_pct": net_pct(eff_sdex_ask, k_bid, 0, config.KRAKEN_FEE_BPS),
        })
    if k_ask and sdex_bid:
        # Direction B: buy on Kraken (ask + fee), sell on SDEX (bid - slippage)
        eff_k_ask = k_ask * (1 + config.KRAKEN_FEE_BPS / 10000)
        eff_sdex_bid = sdex_bid * (1 - config.SLIPPAGE_BPS / 10000)
        rows.append({
            "leg": "Kraken -> SDEX",
            "buy": f"Kraken @ {eff_k_ask:.6f}",
            "sell": f"SDEX @ {eff_sdex_bid:.6f}",
            "net_pct": net_pct(eff_k_ask, eff_sdex_bid, 0, 0),
        })
    if upbit.get("price_usd") and sdex_bid:
        rows.append({
            "leg": "Korea premium (info)",
            "buy": f"SDEX @ {sdex_bid:.6f}",
            "sell": f"Upbit @ {upbit['price_usd']:.6f} (KRW {upbit['price_krw']:.0f})",
            "net_pct": (upbit["price_usd"] - sdex_bid) / sdex_bid * 100,
        })
    return {"rows": rows, "ts": time.time()}


def report(s: dict) -> None:
    print("=" * 64)
    print(f"arb scan @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(s['ts']))}")
    actionable = []
    for row in s["rows"]:
        flag = "ACTIONABLE" if row["net_pct"] >= config.ARB_MIN_NET_PCT else ""
        print(f"  {row['leg']:<18} {row['buy']:<24} -> {row['sell']:<28} "
              f"net {row['net_pct']:+.2f}%  {flag}")
        if flag:
            actionable.append(row)
    if not actionable:
        print("  no actionable spread (>=" f"{config.ARB_MIN_NET_PCT:.1f}% net)")


def main():
    ap = argparse.ArgumentParser(description="XLM CEX<->SDEX arb scanner")
    ap.add_argument("--once", action="store_true", help="single scan (default)")
    ap.add_argument("--watch", action="store_true", help="loop forever")
    args = ap.parse_args()

    if args.watch:
        print(f"[arb] watching every {config.ARB_WATCH_SECONDS:.0f}s. Ctrl-C to stop.")
        while True:
            try:
                report(scan())
            except KeyboardInterrupt:
                print("\n[arb] stopped.")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  [arb] scan error: {e}")
            time.sleep(config.ARB_WATCH_SECONDS)
    else:
        report(scan())


if __name__ == "__main__":
    main()
