"""XLM agent — paper trading loop.

Usage:
  python3 main.py --once     # single iteration (default)
  python3 main.py --loop     # loop forever, POLL_SECONDS between iterations

Everything is virtual: no transactions are ever submitted to any network.
"""
import argparse
import time

import config
import signals
from broker import PaperBroker
from strategy import decide

# prints MoltCanvas status only when it changes, so the loop stays quiet otherwise
_last_molt_status = None


def run_once(broker: PaperBroker) -> None:
    print("=" * 60)
    print(f"iteration @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    molt_up = signals.check_moltcanvas_available()
    global _last_molt_status
    if molt_up != _last_molt_status:
        _last_molt_status = molt_up
        if molt_up:
            print("  [moltcanvas] PLATFORM IS BACK — ready for API key registration!")
        else:
            print("  [moltcanvas] down/unreachable — skipped as sentiment source until it returns")

    sig = signals.collect_signal()
    print("  " + signals.signal_summary(sig))

    decision = decide(sig, broker)
    print(f"  decision: {decision['action'].upper()} — {decision['reason']}")

    mid = sig["mid"] or sig["price_usd"]
    if decision["action"] == "buy" and mid:
        broker.buy(mid, decision["size_usdc"], decision["reason"])
        print(f"  -> virtual BUY {decision['size_usdc']:.2f} USDC worth of XLM @ {mid*(1+config.SLIPPAGE_BPS/10000):.6f}")
    elif decision["action"] == "sell" and mid:
        broker.sell(mid, decision["reason"])
        print(f"  -> virtual SELL all XLM @ {mid*(1-config.SLIPPAGE_BPS/10000):.6f}")

    p = broker.portfolio
    print(f"  portfolio: {p['xlm']:.2f} XLM | {p['usdc']:.2f} USDC | "
          f"equity ~${broker.equity_usd:.2f} | {len(broker.trades)} virtual trades")
    broker.save()


def main():
    parser = argparse.ArgumentParser(description="XLM paper-trading agent")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--once", action="store_true", help="run a single iteration (default)")
    args = parser.parse_args()

    broker = PaperBroker()
    if args.loop:
        print(f"[xlm-agent] paper mode, polling every {config.POLL_SECONDS:.0f}s. Ctrl-C to stop.")
        while True:
            try:
                run_once(broker)
            except KeyboardInterrupt:
                print("\n[xlm-agent] stopped.")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  [xlm-agent] iteration error: {e}")
            time.sleep(config.POLL_SECONDS)
    else:
        run_once(broker)


if __name__ == "__main__":
    main()
