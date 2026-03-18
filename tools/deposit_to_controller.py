#!/usr/bin/env python3
"""
Deposit USDT into Controller to raise the pool above 1M USDT.

Flow:
1. Read DEPLOYER_PRIVATE_KEY from .env.
2. Send a small amount of BNB to USDT_ADDRESS (triggers MockFaucetERC20 receive(), mints 10000 USDT).
3. Call USDT transfer() to send 1000 USDT to CONTROLLER_ADDRESS.

Purpose: verify that above 1M USDT the per-address deposit limit does not apply.

Usage:
  python tools/deposit_to_controller.py --env backend/.env

Optional:
  python tools/deposit_to_controller.py --env backend/.env --amount-usdt 1000
  python tools/deposit_to_controller.py --env backend/.env --faucet-bnb 0.001
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
    v = os.getenv("DEPLOYER_PRIVATE_KEY", "").strip()
    if not v:
        raise RuntimeError("DEPLOYER_PRIVATE_KEY missing in .env")
    if not v.startswith("0x"):
        v = "0x" + v
    if len(v) != 66:
        raise RuntimeError("DEPLOYER_PRIVATE_KEY looks invalid length (expected 32-byte hex)")
    return v


def _to_wei_amount(amount: str, decimals: int) -> int:
    """Convert human-readable amount to wei."""
    d = Decimal(amount)
    if d <= 0:
        raise RuntimeError("amount must be > 0")
    scale = Decimal(10) ** decimals
    return int(d * scale)


def _to_wei_bnb(amount_bnb: str) -> int:
    """Convert BNB amount to wei (18 decimals)."""
    return _to_wei_amount(amount_bnb, 18)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="Path to backend .env (e.g. backend/.env)")
    ap.add_argument("--amount-usdt", default="1000", help="USDT amount to deposit to Controller (default: 1000)")
    ap.add_argument("--faucet-bnb", default="0.001", help="BNB amount to send to USDT faucet (default: 0.001)")
    ap.add_argument("--decimals", type=int, default=18, help="Token decimals (default: 18)")
    ap.add_argument("--wait", action="store_true", help="Wait for tx receipt")
    ap.add_argument("--timeout", type=int, default=180, help="Receipt timeout seconds (default: 180)")
    args = ap.parse_args()

    load_dotenv(args.env, override=True)

    usdt_addr = os.getenv("USDT_ADDRESS", "").strip()
    controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
    if not usdt_addr:
        raise RuntimeError("USDT_ADDRESS missing in .env")
    if not controller_addr:
        raise RuntimeError("CONTROLLER_ADDRESS missing in .env")

    usdt = Web3.to_checksum_address(usdt_addr)
    controller = Web3.to_checksum_address(controller_addr)

    rpc_url = _pick_rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    _inject_poa(w3)
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {rpc_url}")

    pk = _pick_private_key()
    acct = Account.from_key(pk)
    sender = acct.address

    # Check BNB balance
    bnb_bal = w3.eth.get_balance(sender)
    print(f"RPC: {rpc_url}")
    print(f"USDT: {usdt}")
    print(f"Controller: {controller}")
    print(f"Sender: {sender}")
    print(f"BNB balance: {bnb_bal / 1e18:.6f} BNB")

    if bnb_bal < _to_wei_bnb("0.01"):
        print("WARNING: BNB balance is low, may not have enough for gas", file=sys.stderr)

    # USDT ABI (minimal)
    usdt_abi = [
        {
            "inputs": [{"internalType": "address", "name": "", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [
                {"internalType": "address", "name": "to", "type": "address"},
                {"internalType": "uint256", "name": "value", "type": "uint256"},
            ],
            "name": "transfer",
            "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]
    usdt_contract = w3.eth.contract(address=usdt, abi=usdt_abi)

    # Step 1: Send BNB to USDT faucet to claim USDT
    print("\n=== Step 1: Claim USDT from faucet ===")
    faucet_bnb_wei = _to_wei_bnb(args.faucet_bnb)
    usdt_bal_before = usdt_contract.functions.balanceOf(sender).call()
    print(f"USDT balance before: {usdt_bal_before / (10**args.decimals)}")
    print(f"Sending {args.faucet_bnb} BNB to USDT faucet...")

    nonce = w3.eth.get_transaction_count(sender)
    tx_faucet = {
        "from": sender,
        "to": usdt,
        "value": faucet_bnb_wei,
        "nonce": nonce,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }

    signed_faucet = acct.sign_transaction(tx_faucet)
    raw_faucet = getattr(signed_faucet, "rawTransaction", None) or getattr(signed_faucet, "raw_transaction", None)
    if raw_faucet is None:
        raise RuntimeError("SignedTransaction missing raw tx bytes")
    
    txh_faucet = w3.eth.send_raw_transaction(raw_faucet)
    print(f"Faucet txHash: {txh_faucet.hex()}")

    if args.wait:
        receipt_faucet = w3.eth.wait_for_transaction_receipt(txh_faucet, timeout=args.timeout)
        print(f"Faucet receipt.status: {receipt_faucet.status}, blockNumber: {receipt_faucet.blockNumber}")
        if receipt_faucet.status != 1:
            print("ERROR: Faucet transaction failed", file=sys.stderr)
            return 2
        time.sleep(1)  # Small delay for state sync
        usdt_bal_after = usdt_contract.functions.balanceOf(sender).call()
        print(f"USDT balance after: {usdt_bal_after / (10**args.decimals)}")
        print(f"Claimed: {(usdt_bal_after - usdt_bal_before) / (10**args.decimals)} USDT")
    else:
        print("Faucet tx sent. Re-run with --wait to wait for confirmation.")

    # Step 2: Transfer USDT to Controller
    print("\n=== Step 2: Transfer USDT to Controller ===")
    amount_usdt_wei = _to_wei_amount(args.amount_usdt, args.decimals)
    
    # Check if we have enough USDT
    if args.wait:
        usdt_bal_current = usdt_contract.functions.balanceOf(sender).call()
    else:
        usdt_bal_current = usdt_bal_before  # Use before balance if not waiting
    
    if usdt_bal_current < amount_usdt_wei:
        print(f"ERROR: Insufficient USDT balance. Have: {usdt_bal_current / (10**args.decimals)}, Need: {args.amount_usdt}", file=sys.stderr)
        if not args.wait:
            print("NOTE: Run with --wait first to claim USDT, then run again to transfer.", file=sys.stderr)
        return 3

    controller_bal_before = usdt_contract.functions.balanceOf(controller).call()
    print(f"Controller USDT balance before: {controller_bal_before / (10**args.decimals)}")
    print(f"Transferring {args.amount_usdt} USDT to Controller...")

    fn_transfer = usdt_contract.functions.transfer(controller, amount_usdt_wei)
    
    nonce = w3.eth.get_transaction_count(sender)
    try:
        gas_est = fn_transfer.estimate_gas({"from": sender})
        gas = int(gas_est * 12 // 10)  # +20%
    except Exception as e:
        print(f"WARNING: Gas estimation failed: {e}, using default 200000", file=sys.stderr)
        gas = 200_000

    tx_transfer = fn_transfer.build_transaction(
        {
            "from": sender,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )

    signed_transfer = acct.sign_transaction(tx_transfer)
    raw_transfer = getattr(signed_transfer, "rawTransaction", None) or getattr(signed_transfer, "raw_transaction", None)
    if raw_transfer is None:
        raise RuntimeError("SignedTransaction missing raw tx bytes")
    
    txh_transfer = w3.eth.send_raw_transaction(raw_transfer)
    print(f"Transfer txHash: {txh_transfer.hex()}")

    if not args.wait:
        print("Transfer tx sent. Re-run with --wait to wait for confirmation.")
        return 0

    receipt_transfer = w3.eth.wait_for_transaction_receipt(txh_transfer, timeout=args.timeout)
    print(f"Transfer receipt.status: {receipt_transfer.status}, blockNumber: {receipt_transfer.blockNumber}")
    if receipt_transfer.status != 1:
        print("ERROR: Transfer transaction failed", file=sys.stderr)
        return 2

    # Confirm balance
    time.sleep(1)
    controller_bal_after = usdt_contract.functions.balanceOf(controller).call()
    print(f"Controller USDT balance after: {controller_bal_after / (10**args.decimals)}")
    print(f"Delta: {(controller_bal_after - controller_bal_before) / (10**args.decimals)} USDT")
    print("\n✅ Success! USDT deposited to Controller.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
