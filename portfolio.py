"""Daily paper portfolio: timing strategies on a multi-asset basket (keyless).

Each slot gets an equal share of paper capital and runs one rules-based timing
strategy on real Yahoo Finance data:

  QQQ  SMA(10/30)  momentum on tech index
  SPY  SMA(10/30)  momentum on S&P 500
  GLD  RSI(14)     mean reversion on gold
  SLV  RSI(14)     mean reversion on silver

Signal 1 -> hold the asset; signal 0 -> sit in cash. Fills at daily close with
COST_BPS per side (same friction as the lab screen). Virtual money only.

No lookahead: the signal is computed on history up to the *previous* bar, then
filled at the latest close.

Usage:
  .venv/bin/python portfolio.py --once    # one daily check (default)
  .venv/bin/python portfolio.py --loop    # re-check every CHECK_SECONDS
"""
import argparse
import json
import time
from pathlib import Path

import yfinance as yf

from lab import sma_crossover, rsi_meanrev, COST_BPS

STRATS = {"SMA": sma_crossover, "RSI": rsi_meanrev}
SLOTS = [("QQQ", "SMA"), ("SPY", "SMA"), ("GLD", "RSI"), ("SLV", "RSI")]
LOOKBACK = "2y"
WEIGHT = 1.0 / len(SLOTS)
INITIAL_CAPITAL = 1000.0
CHECK_SECONDS = 6 * 3600  # twice a day is plenty for daily-bar signals

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "portfolio_lab.json"
TRADES_FILE = STATE_DIR / "portfolio_lab_trades.json"


def default_state():
    return {
        "cash": {t: INITIAL_CAPITAL * WEIGHT for t, _ in SLOTS},
        "shares": {t: 0.0 for t, _ in SLOTS},
        "signal": {t: 0 for t, _ in SLOTS},
        "entry": {t: None for t, _ in SLOTS},
    }


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return default_state()


def save_state(s):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2))


def log_trade(ticker, side, price, shares, usd):
    STATE_DIR.mkdir(exist_ok=True)
    trades = []
    if TRADES_FILE.exists():
        trades = json.loads(TRADES_FILE.read_text())
    trades.append({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "ticker": ticker, "side": side, "price": round(price, 4),
        "shares": round(shares, 6), "usd": round(usd, 2),
    })
    TRADES_FILE.write_text(json.dumps(trades, indent=2))


def check_once():
    s = load_state()
    equity = 0.0
    lines = []
    for ticker, strat_name in SLOTS:
        df = yf.Ticker(ticker).history(period=LOOKBACK, interval="1d")
        if df.empty or len(df) < 40:
            lines.append(f"  {ticker:5s} no data")
            continue
        px = df["Close"].dropna()
        # signal on everything EXCEPT the latest bar (no lookahead), fill at latest close
        sig = STRATS[strat_name](px.iloc[:-1])
        new_sig = int(sig.iloc[-1]) if len(sig) else 0
        close = float(px.iloc[-1])
        cash, shares = s["cash"][ticker], s["shares"][ticker]

        if new_sig == 1 and shares == 0:
            cost = close * (1 + COST_BPS / 10000)
            shares = cash / cost
            cash = 0.0
            s["entry"][ticker] = close
            log_trade(ticker, "BUY", close, shares, shares * close)
            action = f"BUY @ {close:.2f}"
        elif new_sig == 0 and shares > 0:
            proceeds = shares * close * (1 - COST_BPS / 10000)
            cash = proceeds
            shares = 0.0
            s["entry"][ticker] = None
            log_trade(ticker, "SELL", close, 0, proceeds)
            action = f"SELL @ {close:.2f}"
        else:
            action = "hold" if shares > 0 else "cash"

        s["signal"][ticker] = new_sig
        slot_eq = cash + shares * close
        equity += slot_eq
        lines.append(
            f"  {ticker:5s} {strat_name:3s} sig={new_sig} {action:<14} "
            f"eq=${slot_eq:8.2f}"
        )
        save_state(s)

    pnl = equity - INITIAL_CAPITAL
    print("=" * 64)
    print(f"portfolio check @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("\n".join(lines))
    print(f"  TOTAL equity ${equity:8.2f}  ({pnl:+.2f}% vs ${INITIAL_CAPITAL:.0f} start)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single check (default)")
    ap.add_argument("--loop", action="store_true", help="loop every CHECK_SECONDS")
    args = ap.parse_args()
    if args.loop:
        print(f"[portfolio] watching every {CHECK_SECONDS/3600:.0f}h. Ctrl-C to stop.")
        while True:
            try:
                check_once()
            except KeyboardInterrupt:
                print("\n[portfolio] stopped.")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  [portfolio] error: {e}")
            time.sleep(CHECK_SECONDS)
    else:
        check_once()


if __name__ == "__main__":
    main()
