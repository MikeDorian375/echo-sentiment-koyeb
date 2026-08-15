"""Multi-asset strategy lab: screen simple strategies on real history, keyless.

Data: Yahoo Finance (yfinance) — stocks, ETFs, crypto. No signup, no keys.

Strategies (long-only, daily bars, no lookahead — signal applied next bar):
  1. SMA crossover   — fast SMA > slow SMA -> long, else flat (momentum)
  2. RSI mean reversion — RSI < 30 -> long, RSI > 70 -> flat (buy dips)
  3. Buy & hold      — baseline

Every trade pays COST_BPS per side, so "seems profitable" means profitable
*after* realistic friction. This is a screen, not a promise.

Usage:
  python3 lab.py                                # default universe, 6 months
  python3 lab.py --tickers SPY QQQ --period 1y  # custom
"""
import argparse

import numpy as np
import pandas as pd
import yfinance as yf

COST_BPS = 10  # per-side cost: fees + slippage (0.10%)

# ---------------------------------------------------------------------------
# Signals (all return target exposure 0/1, shifted one bar to avoid lookahead)
# ---------------------------------------------------------------------------

def sma_crossover(px: pd.Series, fast: int = 10, slow: int = 30) -> pd.Series:
    fast_sma = px.rolling(fast).mean()
    slow_sma = px.rolling(slow).mean()
    sig = (fast_sma > slow_sma).astype(int)
    return sig.shift(1).fillna(0)


def rsi_meanrev(px: pd.Series, period: int = 14, buy: float = 30, sell: float = 70) -> pd.Series:
    delta = px.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    sig = pd.Series(0, index=px.index)
    sig[rsi < buy] = 1
    sig[rsi > sell] = 0
    return sig.shift(1).fillna(0)


def buy_and_hold(px: pd.Series) -> pd.Series:
    return pd.Series(1.0, index=px.index)


def ema_crossover(px: pd.Series, fast: int = 12, slow: int = 26) -> pd.Series:
    """EMA crossover (MACD-style trend following)."""
    fe = px.ewm(span=fast, adjust=False).mean()
    se = px.ewm(span=slow, adjust=False).mean()
    return ((fe > se).astype(int)).shift(1).fillna(0)


def macd_signal(px: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    """MACD line crosses above its signal line -> long."""
    macd = px.ewm(span=fast, adjust=False).mean() - px.ewm(span=slow, adjust=False).mean()
    macd_sig = macd.ewm(span=sig, adjust=False).mean()
    return ((macd > macd_sig).astype(int)).shift(1).fillna(0)


def donchian_breakout(px: pd.Series, n: int = 20) -> pd.Series:
    """Donchian channel breakout: close above N-day high -> long until N-day low break."""
    hi = px.rolling(n).max().shift(1)
    lo = px.rolling(n).min().shift(1)
    sig = pd.Series(0, index=px.index)
    pos = 0
    for i in range(len(px)):
        c = px.iloc[i]
        if pos == 0 and c > hi.iloc[i]:
            pos = 1
        elif pos == 1 and c < lo.iloc[i]:
            pos = 0
        sig.iloc[i] = pos
    return sig.shift(1).fillna(0)


def bollinger_meanrev(px: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """Bollinger mean reversion: close below lower band -> long; back above mid -> flat."""
    mid = px.rolling(n).mean()
    sd = px.rolling(n).std()
    lower = mid - k * sd
    sig = pd.Series(0, index=px.index)
    sig[px < lower] = 1
    sig[px > mid] = 0
    return sig.shift(1).fillna(0)


def rsi_trend_filter(px: pd.Series, period: int = 14, buy: float = 40, sell: float = 70,
                     ema_n: int = 50) -> pd.Series:
    """RSI pullback WITH trend filter: only buy dips when price above EMA50 (trend up)."""
    delta = px.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    ema = px.ewm(span=ema_n, adjust=False).mean()
    sig = pd.Series(0, index=px.index)
    sig[(rsi < buy) & (px > ema)] = 1
    sig[rsi > sell] = 0
    return sig.shift(1).fillna(0)


STRATEGIES = {
    "SMA(10/30)": sma_crossover,
    "RSI(14)": rsi_meanrev,
    "EMA(12/26)": ema_crossover,
    "MACD(12/26/9)": macd_signal,
    "Donchian(20)": donchian_breakout,
    "Bollinger(20,2)": bollinger_meanrev,
    "RSI+Trend(40/50)": rsi_trend_filter,
    "Buy&Hold": buy_and_hold,
}

# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def backtest(px: pd.Series, signal: pd.Series) -> dict:
    ret = px.pct_change().fillna(0)
    entries = signal.diff().abs().fillna(signal.iloc[0])  # 1 on each entry
    cost = entries * (COST_BPS / 10000)
    strat_ret = signal * ret - cost
    eq = (1 + strat_ret).cumprod()

    n = len(px)
    total = eq.iloc[-1] - 1
    annual = (eq.iloc[-1] ** (252 / n) - 1) if eq.iloc[-1] > 0 else -1.0
    max_dd = (eq / eq.cummax() - 1).min()
    sharpe = (strat_ret.mean() / strat_ret.std() * np.sqrt(252)) if strat_ret.std() > 0 else 0.0
    win_rate = float((strat_ret > 0).mean())
    return {
        "total%": total * 100,
        "annual%": annual * 100,
        "maxDD%": max_dd * 100,
        "sharpe": sharpe,
        "win%": win_rate * 100,
        "trades": int(entries.sum()),
    }


def screen(tickers: list[str], period: str) -> None:
    rows = []
    for t in tickers:
        df = yf.Ticker(t).history(period=period, interval="1d")
        if df.empty:
            print(f"  {t}: no data")
            continue
        px = df["Close"].dropna()
        for name, fn in STRATEGIES.items():
            try:
                sig = fn(px)
                st = backtest(px, sig)
                rows.append({"asset": t, "strategy": name.strip(), **st})
            except Exception as e:  # noqa: BLE001
                print(f"  {t} {name}: error {e}")
    out = pd.DataFrame(rows)
    if out.empty:
        print("no results")
        return
    pd.set_option("display.width", 120)
    print(f"\n=== Strategy screen: {period}, daily, {COST_BPS}bps/side cost ===")
    print(out.to_string(index=False))
    print("\nInterpretation: green = beat Buy&Hold on this window with positive"
          "\nsharpe. Short window = indicative, not proof. Re-run on 1y/2y before"
          "\nbelieving anything.")


def screen_json(tickers: list[str], period: str) -> dict:
    """JSON variant for the paid API endpoint."""
    rows = []
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period=period, interval="1d")
        except Exception as e:  # noqa: BLE001
            rows.append({"asset": t, "error": str(e)})
            continue
        if df.empty:
            rows.append({"asset": t, "error": "no data"})
            continue
        px = df["Close"].dropna()
        for name, fn in STRATEGIES.items():
            try:
                sig = fn(px)
                st = backtest(px, sig)
                st = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in st.items()}
                rows.append({"asset": t, "strategy": name.strip(), **st})
            except Exception as e:  # noqa: BLE001
                rows.append({"asset": t, "strategy": name.strip(), "error": str(e)})
    return {"tickers": tickers, "period": period, "cost_bps_per_side": COST_BPS,
            "rows": rows, "ts": __import__("time").time()}


def main():
    ap = argparse.ArgumentParser(description="multi-asset strategy screen")
    ap.add_argument("--tickers", nargs="+",
                    default=["SPY", "QQQ", "BTC-USD", "ETH-USD", "GLD", "SLV"])
    ap.add_argument("--period", default="6mo",
                    help="e.g. 6mo, 1y, 2y (yfinance format)")
    args = ap.parse_args()
    screen(args.tickers, args.period)


if __name__ == "__main__":
    main()
