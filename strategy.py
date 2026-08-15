"""Deterministic strategy rules. Sentiment scores; these rules move money.

Rules (all configurable):
  1. Cooldown: no trades within COOLDOWN_MIN of the last one.
  2. Buy: no position AND sentiment >= BUY_THRESHOLD -> spend POSITION_PCT of quote.
  3. Sell: holding AND (sentiment <= SELL_THRESHOLD OR price <= entry*(1-STOP_LOSS_PCT)).
  4. Else: hold.
"""
import time

import config


def decide(signal: dict, broker) -> dict:
    mid = signal.get("mid") or signal.get("price_usd")
    if mid is None:
        return {"action": "hold", "reason": "no usable market price"}

    broker._last_mid = mid  # used for equity calc
    score = signal["sentiment"]
    now = time.time()
    cooldown_left = (broker.portfolio["last_trade_ts"] + config.COOLDOWN_MIN * 60) - now

    if cooldown_left > 0:
        return {"action": "hold", "reason": f"cooldown ({int(cooldown_left/60)}m left)"}

    if not broker.has_position:
        if score >= config.BUY_THRESHOLD:
            size = broker.portfolio["usdc"] * config.POSITION_PCT
            if size < 1.0:
                return {"action": "hold", "reason": "quote balance too small to trade"}
            return {"action": "buy", "size_usdc": size,
                    "reason": f"sentiment {score:+.2f} >= {config.BUY_THRESHOLD}"}
        return {"action": "hold", "reason": f"sentiment {score:+.2f} < buy threshold"}

    # holding a position
    entry = broker.portfolio["entry_price"]
    if entry and mid <= entry * (1 - config.STOP_LOSS_PCT):
        return {"action": "sell",
                "reason": f"stop-loss: mid {mid:.6f} <= entry {entry:.6f} * {(1-config.STOP_LOSS_PCT):.2f}"}
    if score <= config.SELL_THRESHOLD:
        return {"action": "sell", "reason": f"sentiment {score:+.2f} <= {config.SELL_THRESHOLD}"}
    return {"action": "hold", "reason": f"holding; sentiment {score:+.2f}"}
