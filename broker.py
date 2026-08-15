"""Paper broker: virtual balances, simulated fills with slippage, persistent ledger.

No real transactions ever leave this module. Swapping this for a live
stellar-sdk executor later is the entire 'go live' step.
"""
import json
import time
from pathlib import Path

import config


class PaperBroker:
    def __init__(self, state_dir: Path = config.STATE_DIR):
        self.state_dir = state_dir
        self.state_dir.mkdir(exist_ok=True)
        self.portfolio_file = self.state_dir / "portfolio.json"
        self.trades_file = self.state_dir / "trades.json"
        self.portfolio = self._load_portfolio()
        self.trades = self._load_trades()

    # --- persistence -------------------------------------------------------
    def _load_portfolio(self) -> dict:
        if self.portfolio_file.exists():
            return json.loads(self.portfolio_file.read_text())
        return {"usdc": config.INITIAL_QUOTE, "xlm": 0.0, "entry_price": None, "last_trade_ts": 0.0}

    def _load_trades(self) -> list:
        if self.trades_file.exists():
            return json.loads(self.trades_file.read_text())
        return []

    def save(self):
        self.portfolio_file.write_text(json.dumps(self.portfolio, indent=2))
        self.trades_file.write_text(json.dumps(self.trades, indent=2))

    # --- helpers -----------------------------------------------------------
    @property
    def has_position(self) -> bool:
        return self.portfolio["xlm"] > 1e-12

    @property
    def equity_usd(self) -> float:
        # equity uses last known mid; strategy refreshes it before calling
        return self.portfolio["usdc"] + self.portfolio["xlm"] * getattr(self, "_last_mid", 0.0)

    def _record(self, side: str, price: float, xlm: float, usdc: float, reason: str):
        self.trades.append({
            "ts": time.time(),
            "side": side,
            "price": round(price, 8),
            "xlm": round(xlm, 8),
            "usdc": round(usdc, 8),
            "reason": reason,
        })
        self.save()

    # --- fills -------------------------------------------------------------
    def buy(self, mid: float, usdc_amount: float, reason: str) -> dict:
        """Spend usdc_amount (USD) to buy XLM at mid + slippage."""
        slip = 1 + config.SLIPPAGE_BPS / 10000
        exec_price = mid * slip
        xlm = usdc_amount / exec_price
        self.portfolio["usdc"] -= usdc_amount
        self.portfolio["xlm"] += xlm
        self.portfolio["entry_price"] = exec_price
        self.portfolio["last_trade_ts"] = time.time()
        self._record("BUY", exec_price, xlm, usdc_amount, reason)
        return {"xlm": xlm, "price": exec_price, "usdc": usdc_amount}

    def sell(self, mid: float, reason: str) -> dict:
        """Sell entire XLM position at mid - slippage."""
        slip = 1 - config.SLIPPAGE_BPS / 10000
        exec_price = mid * slip
        xlm = self.portfolio["xlm"]
        usdc = xlm * exec_price
        self.portfolio["usdc"] += usdc
        self.portfolio["xlm"] = 0.0
        self.portfolio["entry_price"] = None
        self.portfolio["last_trade_ts"] = time.time()
        self._record("SELL", exec_price, xlm, usdc, reason)
        return {"xlm": xlm, "price": exec_price, "usdc": usdc}
