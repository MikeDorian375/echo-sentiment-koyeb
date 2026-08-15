"""Solana green-path runner: execute a PAID x402 request via the Solana rail.

Prereq: the SOL test client (state/sol_test_client.txt) must hold mainnet
USDC (SPL). Usage:
  .venv/bin/python solana_green_path.py [URL] [ENDPOINT]
"""
import json
import os
import sys

import requests
from solders.keypair import Keypair

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ENDPOINT = sys.argv[2] if len(sys.argv) > 2 else "/v1/sentiment"
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")

KP = Keypair.from_json(open("state/sol_test_client.txt").read())
print("SOL client:", KP.pubkey())
print("rail: solana mainnet USDC | endpoint:", URL + ENDPOINT)

from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.svm.exact import register_exact_svm_client
from x402.mechanisms.svm.signers import KeypairSigner

client = x402ClientSync()
register_exact_svm_client(client, KeypairSigner(KP), networks="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", rpc_url=SOL_RPC)
http_client = x402HTTPClientSync(client)

r1 = requests.get(f"{URL}{ENDPOINT}", timeout=30)
print(f"[1] GET {ENDPOINT} (no payment) -> HTTP {r1.status_code}")
if r1.status_code != 402:
    print("expected 402 — got", r1.status_code, r1.text[:200])
    sys.exit(1)

# Show rails
try:
    import base64
    pr = json.loads(base64.b64decode(r1.headers.get("payment-required", "") + "=="))
    for a in pr.get("accepts", []):
        print("   rail:", a.get("network"), "| payTo:", str(a.get("payTo"))[:14], "| amount:", a.get("amount"))
except Exception:
    pass

headers, payload = http_client.handle_402_response(r1.headers, r1.content)
print("[2] payment payload signed (Solana)")

r2 = requests.get(f"{URL}{ENDPOINT}", headers=headers, timeout=120)
print(f"[3] retry with PAYMENT-SIGNATURE -> HTTP {r2.status_code}")

if r2.status_code == 200:
    print("\n*** SOLANA PAID ROUND TRIP SUCCESS ***")
    print(r2.text[:400])
else:
    try:
        settle = http_client.get_payment_settle_response(lambda n: r2.headers.get(n))
        print(f"[4] rejected: success={settle.success} reason={settle.error_reason}")
    except Exception as e:  # noqa: BLE001
        print(f"[4] rejected (no settle header: {e})")
