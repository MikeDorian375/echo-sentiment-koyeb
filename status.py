"""Status dashboard — all wallets + fleet health in one glance.

Usage:  .venv/bin/python status.py
"""
import json
import os
import urllib.request
from pathlib import Path

BASE_RPC = os.environ.get("BASE_RPC", "https://mainnet.base.org")
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

WALLETS = {
    "J revenue (Base)":     ("evm", "0x583FfEE3f6E0E8cAB3531fBd5C4e291784D3b6cD", "usdc"),
    "Gas key (Base)":       ("evm", "0x49445b14b6196017bBF23628a8fF3F3472a5Eec4", "eth"),
    "Test client (Base)":   ("evm", "0xe929413E5eC28f914B67a6219a60a5Dd6C97868C", "both"),
    "J revenue (Solana)":   ("sol", "7bu8aB2w94N8TRysqbBdKXNoPqSr9UopaZXJGVSRbLgk", "usdc"),
    "Gas key (Solana)":     ("sol", "BphPjLq9Z2p7fV3tQzpfhbghD1gCocmdC8V8WkGm7Lav", "sol"),
    "Test client (Solana)": ("sol", "iiUvsdTznF3D1je5Qca11Wj9EVhde2qemMQWjT6S9EM", "usdc"),
}

# --- EVM helpers -----------------------------------------------------------
def evm_rpc(method, params):
    req = urllib.request.Request(BASE_RPC,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=15))["result"]

def evm_eth(addr):
    return int(evm_rpc("eth_getBalance", [addr, "latest"]), 16) / 1e18

def evm_usdc(addr):
    data = "0x70a08231" + "0" * 24 + addr[2:].lower()
    return int(evm_rpc("eth_call", [{"to": BASE_USDC, "data": data}, "latest"]), 16) / 1e6

# --- Solana helpers --------------------------------------------------------
def sol_client():
    try:
        from solana.rpc.api import Client
    except ImportError:
        raise RuntimeError("solana lib not found — run with ./.venv/bin/python status.py")
    return Client(SOL_RPC)

def sol_balance(client, addr, kind):
    from solders.pubkey import Pubkey
    from solana.rpc.types import TokenAccountOpts
    pub = Pubkey.from_string(addr)
    if kind == "sol":
        return client.get_balance(pub).value / 1e9
    accts = client.get_token_accounts_by_owner(pub, TokenAccountOpts(mint=Pubkey.from_string(SOL_USDC_MINT))).value
    if not accts:
        return 0.0
    return sum((client.get_token_account_balance(a.pubkey).value.ui_amount) or 0 for a in accts)

# --- Fleet health ----------------------------------------------------------
def http_code(url, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None

def main():
    print("=" * 62)
    print("ECHO SENTIMENT — STATUS DASHBOARD")
    print("=" * 62)
    print("\n[WALLETS]")
    print(f"{'':32s} {'balance':>16s}")
    print("-" * 50)
    for name, (chain, addr, kind) in WALLETS.items():
        try:
            if chain == "evm":
                if kind in ("eth", "both"):
                    eth = evm_eth(addr)
                if kind in ("usdc", "both"):
                    usdc = evm_usdc(addr)
                parts = []
                if kind in ("eth", "both"):
                    parts.append(f"{eth:.4f} ETH")
                if kind in ("usdc", "both"):
                    parts.append(f"{usdc:.4f} USDC")
                print(f"{name:32s} {'  '.join(parts):>16s}")
            else:
                c = sol_client()
                if kind == "sol":
                    print(f"{name:32s} {sol_balance(c, addr, 'sol'):>10.6f} SOL")
                else:
                    print(f"{name:32s} {sol_balance(c, addr, 'usdc'):>10.4f} USDC")
        except Exception as e:
            print(f"{name:32s} ERROR: {str(e)[:40]}")

    print("\n[FLEET]")
    checks = [
        ("api_server (local)", "http://127.0.0.1:8000/", None),
        ("mcp_bridge (local)", "http://127.0.0.1:8010/mcp", None),
        ("public /v1/sample", "https://api.6766587364.lol/v1/sample",
         {"User-Agent": "python-requests/2.32.0"}),
        ("public /v1/sentiment", "https://api.6766587364.lol/v1/sentiment",
         {"User-Agent": "python-requests/2.32.0"}),
    ]
    for name, url, hdrs in checks:
        code = http_code(url, hdrs)
        if name == "mcp_bridge (local)":
            # bridge serves at /mcp — any HTTP response means it's alive
            note = "OK" if code is not None else "DOWN"
        else:
            note = "PAYWALL OK" if code == 402 else ("OK" if code == 200 else "DOWN")
        print(f"  {name:28s} HTTP {str(code):>4s}  {note}")

    print("\n[DISCOVERY]")
    for name, url in [
        ("llms.txt", "https://api.6766587364.lol/llms.txt"),
        ("openapi", "https://api.6766587364.lol/openapi.json"),
        ("spec.pdf", "https://api.6766587364.lol/spec.pdf"),
        ("robots.txt", "https://api.6766587364.lol/robots.txt"),
        ("x402 manifest", "https://api.6766587364.lol/.well-known/x402"),
    ]:
        code = http_code(url, {"User-Agent": "python-requests/2.32.0"})
        print(f"  {name:28s} HTTP {str(code):>4s}  {'OK' if code == 200 else 'DOWN'}")

if __name__ == "__main__":
    main()
