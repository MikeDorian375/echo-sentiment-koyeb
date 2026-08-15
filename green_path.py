"""Green-path runner: check throwaway wallet, then execute a PAID x402 request.

Prereq: the throwaway wallet (state/testnet_client_wallet.txt) must hold
Base Sepolia testnet USDC. Get it free at https://faucet.circle.com
(address + chain Base Sepolia + asset USDC; no real money involved).

Usage:
  .venv/bin/python green_path.py [URL]
"""
import json
import os
import sys
import urllib.request

import requests

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ENDPOINT = sys.argv[2] if len(sys.argv) > 2 else "/v1/sentiment"
WALLET_FILE = "state/testnet_client_wallet.txt"

lines = open(WALLET_FILE).read().strip().splitlines()
ADDR, KEY = lines[0].strip(), lines[1].strip()

RPC = os.environ.get("BASE_RPC", "https://mainnet.base.org")
USDC = os.environ.get("USDC_ADDR", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")  # Base mainnet USDC


def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


print("=" * 64)
print("GREEN PATH — paid x402 round trip")
print("=" * 64)
print(f"\nwallet: {ADDR}")

eth = int(rpc("eth_getBalance", [ADDR, "latest"])["result"], 16)
data = "0x70a08231" + "0" * 24 + ADDR[2:].lower()
usdc_bal = int(rpc("eth_call", [{"to": USDC, "data": data}, "latest"])["result"], 16)
print(f"ETH: {eth / 1e18:.6f} | USDC: {usdc_bal / 1e6:.4f}")

if usdc_bal < 1_000_000:  # < 1 USDC
    print("\nNOT FUNDED — drip testnet USDC at https://faucet.circle.com")
    print(f"  address: {ADDR} | chain: Base Sepolia | asset: USDC")
    sys.exit(1)

print("\nfunded! running paid request...\n")

from eth_account import Account  # noqa: E402

from x402 import x402ClientSync  # noqa: E402
from x402.http import x402HTTPClientSync  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_client  # noqa: E402
from x402.mechanisms.evm.signers import EthAccountSigner  # noqa: E402

client = x402ClientSync()
register_exact_evm_client(client, EthAccountSigner(Account.from_key(KEY)), networks="eip155:*")
http_client = x402HTTPClientSync(client)

r1 = requests.get(f"{URL}{ENDPOINT}", timeout=30)
print(f"[1] GET {ENDPOINT} (no payment) -> HTTP {r1.status_code}")

headers, payload = http_client.handle_402_response(r1.headers, r1.content)
print(f"[2] payment payload signed")

r2 = requests.get(f"{URL}{ENDPOINT}", headers=headers, timeout=90)
print(f"[3] retry with PAYMENT-SIGNATURE -> HTTP {r2.status_code}")

if r2.status_code == 200:
    print("\n*** PAID ROUND TRIP SUCCESS ***")
    print(r2.text[:500])
else:
    try:
        settle = http_client.get_payment_settle_response(lambda n: r2.headers.get(n))
        print(f"\n[4] rejected: success={settle.success} reason={settle.error_reason}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[4] rejected (no settle header: {e})")
