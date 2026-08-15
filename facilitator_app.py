"""Self-hosted x402 facilitator — verifies & settles payments on-chain.

Runs as its own FastAPI app (own port, own event loop) so the resource
server can call it over HTTP without self-deadlocking. This is what unlocks
MAINNET payments (the public x402.org facilitator is testnet-only).

Endpoints: POST /verify, POST /settle, GET /supported.

Gas wallet: generated once, stored in state/gas_key.txt. Fund it with a small
amount of Base ETH (mainnet) — it pays settlement gas; client funds go
straight to PAY_TO, never through this box.
"""
import os
from pathlib import Path

from eth_account import Account
from fastapi import FastAPI
from pydantic import BaseModel

from x402 import x402Facilitator
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.exact import register_exact_evm_facilitator
from x402.mechanisms.svm.signers import FacilitatorKeypairSigner
from x402.mechanisms.svm.exact import register_exact_svm_facilitator
from x402.schemas import parse_payment_payload, parse_payment_requirements

NETWORK = os.environ.get("NETWORK", "eip155:84532")
BASE_RPC = os.environ.get(
    "BASE_RPC",
    "https://mainnet.base.org" if NETWORK.endswith(":8453") else "https://base-sepolia.publicnode.com",
)
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")
SOL_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

gas_file = Path(__file__).resolve().parent / "state" / "gas_key.txt"
GAS_KEY = os.environ.get("GAS_KEY", "")
if not GAS_KEY:
    if gas_file.exists():
        GAS_KEY = gas_file.read_text().strip()
    else:
        GAS_KEY = Account.create().key.hex()
        gas_file.parent.mkdir(exist_ok=True)
        gas_file.write_text(GAS_KEY)
        print(f"*** NEW gas key generated -> {gas_file} (FUND IT with Base ETH before live use)")

GAS_ADDRESS = Account.from_key(GAS_KEY).address

facilitator = x402Facilitator()
register_exact_evm_facilitator(facilitator, FacilitatorWeb3Signer(GAS_KEY, BASE_RPC), networks=NETWORK)

# --- Solana rail (USDC on Solana mainnet) ---
from solders.keypair import Keypair as SolKeypair

_sol_gas_file = Path(__file__).resolve().parent / "state" / "sol_gas_key.txt"
if _sol_gas_file.exists():
    SOL_GAS_KEYPAIR = SolKeypair.from_json(_sol_gas_file.read_text())
else:
    SOL_GAS_KEYPAIR = SolKeypair()
    _sol_gas_file.parent.mkdir(exist_ok=True)
    _sol_gas_file.write_text(SOL_GAS_KEYPAIR.to_json())
    print(f"*** NEW solana gas keypair -> {_sol_gas_file} (FUND with SOL before live use)")
SOL_GAS_ADDRESS = str(SOL_GAS_KEYPAIR.pubkey())
register_exact_svm_facilitator(
    facilitator,
    FacilitatorKeypairSigner([SOL_GAS_KEYPAIR], SOL_RPC),
    networks=SOL_MAINNET_CAIP2,
)

app = FastAPI(title="Echo x402 Facilitator", version="0.1.0")


class _PayReq(BaseModel):
    paymentPayload: dict
    paymentRequirements: dict


@app.post("/verify")
async def verify(req: _PayReq):
    payload = parse_payment_payload(req.paymentPayload)
    requirements = parse_payment_requirements(payload.x402_version, req.paymentRequirements)
    resp = await facilitator.verify(payload, requirements)
    return resp.model_dump(by_alias=True, exclude_none=True)


@app.post("/settle")
async def settle(req: _PayReq):
    payload = parse_payment_payload(req.paymentPayload)
    requirements = parse_payment_requirements(payload.x402_version, req.paymentRequirements)
    resp = await facilitator.settle(payload, requirements)
    return resp.model_dump(by_alias=True, exclude_none=True)


@app.get("/supported")
async def supported():
    resp = facilitator.get_supported()
    return {
        "kinds": [k.model_dump(by_alias=True, exclude_none=True) for k in resp.kinds],
        "extensions": resp.extensions,
        "signers": resp.signers,
    }


@app.get("/health")
async def health():
    return {"ok": True, "network": NETWORK, "gas_wallet": GAS_ADDRESS, "sol_gas_wallet": SOL_GAS_ADDRESS}


def start_in_thread(port: int = 4022):
    """Start this facilitator app in a background thread (own event loop)."""
    import threading

    import uvicorn

    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    print(f"[facilitator] serving on 127.0.0.1:{port} | network {NETWORK} | gas {GAS_ADDRESS}")
    return srv


if __name__ == "__main__":
    import uvicorn

    print(f"[facilitator] standalone on :4022 | network {NETWORK} | gas {GAS_ADDRESS}")
    uvicorn.run(app, host="127.0.0.1", port=4022, log_level="warning")
