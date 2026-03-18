#!/usr/bin/env python3
"""
Mint initial pool USDT (MockFaucetERC20) to Controller on BSC testnet.

This is a testnet-only remediation tool:
- Uses a private key from .env to send a tx
- Calls MockFaucetERC20.mint(to, amount)

Default:
- to = CONTROLLER_ADDRESS
- amount = 100_000e18

Usage:
  python tools/mint_initial_pool_usdt.py --env backend/.env

Optional:
  python tools/mint_initial_pool_usdt.py --env backend/.env --amount-usdt 100000
  python tools/mint_initial_pool_usdt.py --env backend/.env --to 0x...
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware  # web3==7.x


def _inject_poa(w3: Web3) -> None:
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _pick_rpc_url() -> str:
    use_quicknode = _env_bool("USE_QUICKNODE", False)
    if use_quicknode:
        url = os.getenv("QUICKNODE_HTTP_URL", "").strip()
        if not url:
            raise RuntimeError("USE_QUICKNODE=true but QUICKNODE_HTTP_URL is empty")
        return url
    return os.getenv("BSC_RPC_URL", "https://data-seed-prebsc-1-s1.binance.org:8545/").strip()


def _pick_private_key() -> str:
    # Prefer an explicit minter key if provided; else fallback to KEEPER/DEPLOYER.
    for k in ("MINTER_PRIVATE_KEY", "KEEPER_PRIVATE_KEY", "DEPLOYER_PRIVATE_KEY"):
        v = os.getenv(k, "").strip()
        if v:
            if not v.startswith("0x"):
                v = "0x" + v
            if len(v) != 66:
                raise RuntimeError(f"{k} looks invalid length (expected 32-byte hex)")
            return v
    raise RuntimeError("Missing MINTER_PRIVATE_KEY / KEEPER_PRIVATE_KEY / DEPLOYER_PRIVATE_KEY in .env")


def _to_wei_amount(amount_usdt: str, decimals: int) -> int:
    # Accept "100000" or "100000.0"
    d = Decimal(amount_usdt)
    if d <= 0:
        raise RuntimeError("amount must be > 0")
    scale = Decimal(10) ** decimals
    return int(d * scale)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="Path to backend .env (e.g. backend/.env)")
    ap.add_argument("--to", default=None, help="Recipient address (default: CONTROLLER_ADDRESS)")
    ap.add_argument("--amount-usdt", default="100000", help="USDT amount in human units (default: 100000)")
    ap.add_argument("--decimals", type=int, default=18, help="Token decimals (default: 18)")
    ap.add_argument("--wait", action="store_true", help="Wait for tx receipt")
    ap.add_argument("--timeout", type=int, default=180, help="Receipt timeout seconds (default: 180)")
    args = ap.parse_args()

    load_dotenv(args.env, override=True)

    usdt_addr = os.getenv("USDT_ADDRESS", "").strip()
    controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
    if not usdt_addr:
        raise RuntimeError("USDT_ADDRESS missing in .env")
    if not controller_addr and not args.to:
        raise RuntimeError("CONTROLLER_ADDRESS missing in .env (or pass --to)")

    usdt = Web3.to_checksum_address(usdt_addr)
    to = Web3.to_checksum_address(args.to or controller_addr)

    rpc_url = _pick_rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    _inject_poa(w3)
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {rpc_url}")

    pk = _pick_private_key()
    acct = Account.from_key(pk)
    sender = acct.address

    amount_wei = _to_wei_amount(args.amount_usdt, args.decimals)

    # Minimal ABI for MockFaucetERC20
    abi = [
        {
            "inputs": [
                {"internalType": "address", "name": "to", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
            ],
            "name": "mint",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [{"internalType": "address", "name": "", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]
    token = w3.eth.contract(address=usdt, abi=abi)

    bal_before = token.functions.balanceOf(to).call()
    print(f"RPC: {rpc_url}")
    print(f"USDT: {usdt}")
    print(f"Controller(to): {to}")
    print(f"Sender: {sender}")
    print(f"Controller balance before: {bal_before / (10**args.decimals)}")
    print(f"Mint amount: {Decimal(args.amount_usdt)}")

    fn = token.functions.mint(to, amount_wei)

    # Build tx
    nonce = w3.eth.get_transaction_count(sender)
    try:
        gas_est = fn.estimate_gas({"from": sender})
        gas = int(gas_est * 12 // 10)  # +20%
    except Exception:
        gas = 200_000

    tx = fn.build_transaction(
        {
            "from": sender,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )

    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        raise RuntimeError("SignedTransaction missing raw tx bytes (rawTransaction/raw_transaction)")
    txh = w3.eth.send_raw_transaction(raw)
    print(f"txHash: {txh.hex()}")

    if not args.wait:
        print("Sent. Re-run with --wait to wait for confirmation.")
        return 0

    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=args.timeout)
    print(f"receipt.status: {receipt.status}, blockNumber: {receipt.blockNumber}")
    if receipt.status != 1:
        return 2

    # Confirm balance
    # Some nodes may lag; small delay for indexers isn't needed for on-chain call, but keep tiny sleep for safety.
    time.sleep(1)
    bal_after = token.functions.balanceOf(to).call()
    print(f"Controller balance after: {bal_after / (10**args.decimals)}")
    print(f"Delta: {(bal_after - bal_before) / (10**args.decimals)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)

