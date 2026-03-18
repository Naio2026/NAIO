#!/usr/bin/env python3
"""
Batch-deposit USDT into Controller to raise the pool above 1M USDT.

Flow:
1. Generate N new addresses and private keys.
2. For each address:
   a. Transfer a small amount of BNB from DEPLOYER_PRIVATE_KEY to the new address (for gas).
   b. Send a small amount of BNB to USDT_ADDRESS (triggers MockFaucetERC20 receive(), mints 10000 USDT).
   c. Call USDT transfer() to send 1000 USDT to CONTROLLER_ADDRESS.

Purpose: verify that above 1M USDT the per-address deposit limit does not apply.

Usage:
  python tools/batch_deposit_to_controller.py --env backend/.env --count 10

Optional:
  python tools/batch_deposit_to_controller.py --env backend/.env --count 100 --amount-usdt 1000 --faucet-bnb 0.001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from typing import List, Tuple

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


def generate_accounts(count: int) -> List[Tuple[str, str]]:
    """Generate N new accounts (address, private_key)."""
    accounts = []
    for i in range(count):
        acct = Account.create()
        accounts.append((acct.address, acct.key.hex()))
    return accounts


def send_bnb(w3: Web3, from_acct: Account, to_address: str, amount_wei: int, nonce: int) -> bytes:
    """Send BNB from one account to another."""
    tx = {
        "from": from_acct.address,
        "to": Web3.to_checksum_address(to_address),
        "value": amount_wei,
        "nonce": nonce,
        "gas": 21_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    signed = from_acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        raise RuntimeError("SignedTransaction missing raw tx bytes")
    return raw


def claim_usdt_from_faucet(w3: Web3, acct: Account, usdt_address: str, faucet_bnb_wei: int, nonce: int) -> bytes:
    """Send BNB to USDT faucet to claim USDT."""
    tx = {
        "from": acct.address,
        "to": Web3.to_checksum_address(usdt_address),
        "value": faucet_bnb_wei,
        "nonce": nonce,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        raise RuntimeError("SignedTransaction missing raw tx bytes")
    return raw


def transfer_usdt_to_controller(
    w3: Web3, acct: Account, usdt_contract, controller_address: str, amount_wei: int, nonce: int
) -> bytes:
    """Transfer USDT to Controller."""
    fn = usdt_contract.functions.transfer(Web3.to_checksum_address(controller_address), amount_wei)
    try:
        gas_est = fn.estimate_gas({"from": acct.address})
        gas = int(gas_est * 12 // 10)  # +20%
    except Exception:
        gas = 200_000

    tx = fn.build_transaction(
        {
            "from": acct.address,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        raise RuntimeError("SignedTransaction missing raw tx bytes")
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="Path to backend .env (e.g. backend/.env)")
    ap.add_argument("--count", type=int, default=None, help="Number of addresses to generate and deposit (if not set, will auto-run until target pool reached)")
    ap.add_argument("--target-pool", default="1000000", help="Target pool USDT amount (default: 1000000, i.e. 1M USDT)")
    ap.add_argument("--amount-usdt", default="1000", help="USDT amount to deposit per address (default: 1000)")
    ap.add_argument("--faucet-bnb", default="0.001", help="BNB amount to send to USDT faucet (default: 0.001)")
    ap.add_argument("--gas-bnb", default=None, help="BNB amount to send to each new address for gas (default: auto-calculated as faucet-bnb + 0.002)")
    ap.add_argument("--decimals", type=int, default=18, help="Token decimals (default: 18)")
    ap.add_argument("--wait", action="store_true", help="Wait for tx receipt")
    ap.add_argument("--timeout", type=int, default=180, help="Receipt timeout seconds (default: 180)")
    ap.add_argument("--save-keys", action="store_true", help="Save generated keys to batch_deposit_keys.json")
    ap.add_argument("--batch-size", type=int, default=10, help="Number of addresses to process in each batch (default: 10)")
    args = ap.parse_args()

    if args.count is not None and args.count <= 0:
        raise RuntimeError("--count must be > 0 if specified")
    
    if args.batch_size <= 0:
        raise RuntimeError("--batch-size must be > 0")
    
    target_pool_wei = _to_wei_amount(args.target_pool, args.decimals)

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

    deployer_pk = _pick_private_key()
    deployer_acct = Account.from_key(deployer_pk)
    deployer_address = deployer_acct.address

    # USDT ABI (minimal) - define early so we can check pool balance
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
    amount_usdt_wei = _to_wei_amount(args.amount_usdt, args.decimals)

    # Calculate gas_bnb first (needed for required_bnb calculation)
    faucet_bnb_wei = _to_wei_bnb(args.faucet_bnb)
    if args.gas_bnb is None:
        # Auto-calculate: faucet_bnb + gas buffer (0.002 BNB for 2 transactions)
        gas_bnb_wei = faucet_bnb_wei + _to_wei_bnb("0.002")
    else:
        gas_bnb_wei = _to_wei_bnb(args.gas_bnb)

    # Check deployer BNB balance
    deployer_bnb = w3.eth.get_balance(deployer_address)
    
    # Estimate required BNB
    if args.count is not None:
        required_bnb = (gas_bnb_wei + faucet_bnb_wei) * args.count
        print(f"RPC: {rpc_url}")
        print(f"USDT: {usdt}")
        print(f"Controller: {controller}")
        print(f"Deployer: {deployer_address}")
        print(f"Deployer BNB balance: {deployer_bnb / 1e18:.6f} BNB")
        print(f"Required BNB: {required_bnb / 1e18:.6f} BNB")
    else:
        # Auto mode: estimate based on target pool
        current_bal = usdt_contract.functions.balanceOf(controller).call()
        needed_usdt = max(0, target_pool_wei - current_bal)
        estimated_addresses = int(needed_usdt / amount_usdt_wei) + 10  # +10 for safety
        required_bnb = (gas_bnb_wei + faucet_bnb_wei) * estimated_addresses
        print(f"RPC: {rpc_url}")
        print(f"USDT: {usdt}")
        print(f"Controller: {controller}")
        print(f"Deployer: {deployer_address}")
        print(f"Deployer BNB balance: {deployer_bnb / 1e18:.6f} BNB")
        print(f"Current pool: {current_bal / (10**args.decimals):.2f} USDT")
        print(f"Target pool: {args.target_pool} USDT")
        print(f"Estimated required BNB: {required_bnb / 1e18:.6f} BNB (for ~{estimated_addresses} addresses)")
    
    if args.gas_bnb is None:
        print(f"Gas BNB per address (auto): {gas_bnb_wei / 1e18:.6f} BNB (faucet: {args.faucet_bnb} + gas buffer: 0.002)")
    else:
        print(f"Gas BNB per address: {gas_bnb_wei / 1e18:.6f} BNB")

    if deployer_bnb < required_bnb:
        print(f"WARNING: Deployer BNB balance may be insufficient. Need: {required_bnb / 1e18:.6f} BNB", file=sys.stderr)

    # Get initial controller balance
    controller_bal_before = usdt_contract.functions.balanceOf(controller).call()
    print(f"\nController USDT balance before: {controller_bal_before / (10**args.decimals)}")
    print(f"Target pool: {args.target_pool} USDT")
    
    if args.count is not None:
        print(f"\nStarting batch deposit for {args.count} addresses...\n")
    else:
        print(f"\nStarting auto batch deposit until pool reaches {args.target_pool} USDT...\n")
        print(f"Batch size: {args.batch_size} addresses per batch\n")

    deployer_nonce = w3.eth.get_transaction_count(deployer_address)
    success_count = 0
    failed_count = 0
    all_accounts = []  # Store all generated accounts for saving keys
    
    # Determine if we should run until target or fixed count
    run_until_target = args.count is None
    
    if run_until_target:
        # Auto mode: generate and process in batches until target reached
        batch_num = 0
        while True:
            batch_num += 1
            # Check current pool balance
            current_bal = usdt_contract.functions.balanceOf(controller).call()
            current_bal_usdt = current_bal / (10**args.decimals)
            
            if current_bal >= target_pool_wei:
                print(f"\n{'='*60}")
                print(f"✅ Target pool reached! Current: {current_bal_usdt:.2f} USDT (target: {args.target_pool} USDT)")
                break
            
            needed_usdt = (target_pool_wei - current_bal) / (10**args.decimals)
            estimated_addresses = int(needed_usdt / float(args.amount_usdt)) + 1
            batch_count = min(args.batch_size, estimated_addresses)
            
            print(f"\n[Batch {batch_num}] Current pool: {current_bal_usdt:.2f} USDT, Need: {needed_usdt:.2f} USDT")
            print(f"  Generating {batch_count} addresses for this batch...")
            
            # Generate accounts for this batch
            batch_accounts = generate_accounts(batch_count)
            all_accounts.extend(batch_accounts)
            
            # Process this batch
            for i, (new_address, new_pk) in enumerate(batch_accounts, 1):
                total_i = success_count + failed_count + i
                try:
                    print(f"[{total_i}] Processing {new_address}...")
                    new_acct = Account.from_key(new_pk)

                    # Step 1: Send BNB from deployer to new address (for gas)
                    print(f"  → Sending {gas_bnb_wei / 1e18:.6f} BNB for gas...")
                    raw_send_bnb = send_bnb(w3, deployer_acct, new_address, gas_bnb_wei, deployer_nonce)
                    txh_send_bnb = w3.eth.send_raw_transaction(raw_send_bnb)
                    deployer_nonce += 1
                    if args.wait:
                        receipt = w3.eth.wait_for_transaction_receipt(txh_send_bnb, timeout=args.timeout)
                        if receipt.status != 1:
                            print(f"  ✗ Failed to send BNB: {txh_send_bnb.hex()}")
                            failed_count += 1
                            continue
                        time.sleep(0.5)  # Small delay
                    else:
                        print(f"  → Gas txHash: {txh_send_bnb.hex()}")

                    # Step 2: Claim USDT from faucet
                    print(f"  → Claiming USDT from faucet...")
                    new_nonce = 0
                    raw_faucet = claim_usdt_from_faucet(w3, new_acct, usdt, faucet_bnb_wei, new_nonce)
                    txh_faucet = w3.eth.send_raw_transaction(raw_faucet)
                    new_nonce += 1
                    if args.wait:
                        receipt = w3.eth.wait_for_transaction_receipt(txh_faucet, timeout=args.timeout)
                        if receipt.status != 1:
                            print(f"  ✗ Failed to claim USDT: {txh_faucet.hex()}")
                            failed_count += 1
                            continue
                        time.sleep(0.5)
                    else:
                        print(f"  → Faucet txHash: {txh_faucet.hex()}")

                    # Step 3: Transfer USDT to Controller
                    print(f"  → Transferring {args.amount_usdt} USDT to Controller...")
                    raw_transfer = transfer_usdt_to_controller(w3, new_acct, usdt_contract, controller, amount_usdt_wei, new_nonce)
                    txh_transfer = w3.eth.send_raw_transaction(raw_transfer)
                    if args.wait:
                        receipt = w3.eth.wait_for_transaction_receipt(txh_transfer, timeout=args.timeout)
                        if receipt.status != 1:
                            print(f"  ✗ Failed to transfer USDT: {txh_transfer.hex()}")
                            failed_count += 1
                            continue
                        time.sleep(0.5)
                        
                        # Check pool balance after each successful deposit
                        current_bal = usdt_contract.functions.balanceOf(controller).call()
                        if current_bal >= target_pool_wei:
                            print(f"  ✓ Success: {new_address}")
                            print(f"\n{'='*60}")
                            print(f"✅ Target pool reached! Current: {current_bal / (10**args.decimals):.2f} USDT")
                            success_count += 1
                            break
                    else:
                        print(f"  → Transfer txHash: {txh_transfer.hex()}")

                    print(f"  ✓ Success: {new_address}")
                    success_count += 1

                    # Small delay between addresses to avoid nonce issues
                    if not args.wait:
                        time.sleep(0.2)

                except Exception as e:
                    print(f"  ✗ Error processing {new_address}: {e}", file=sys.stderr)
                    failed_count += 1
                    continue
            
            # If we broke out of the inner loop due to target reached, break outer loop too
            if args.wait:
                current_bal = usdt_contract.functions.balanceOf(controller).call()
                if current_bal >= target_pool_wei:
                    break
            
            # Small delay between batches
            if not args.wait:
                time.sleep(1)
        
        accounts = all_accounts
    else:
        # Fixed count mode: use pre-generated accounts
        accounts = generate_accounts(args.count)
        if args.save_keys:
            all_accounts = accounts

    # Process accounts in fixed count mode
    if not run_until_target:
        for i, (new_address, new_pk) in enumerate(accounts, 1):
            try:
                print(f"[{i}/{args.count}] Processing {new_address}...")
                new_acct = Account.from_key(new_pk)

                # Step 1: Send BNB from deployer to new address (for gas)
                print(f"  → Sending {gas_bnb_wei / 1e18:.6f} BNB for gas...")
                raw_send_bnb = send_bnb(w3, deployer_acct, new_address, gas_bnb_wei, deployer_nonce)
                txh_send_bnb = w3.eth.send_raw_transaction(raw_send_bnb)
                deployer_nonce += 1
                if args.wait:
                    receipt = w3.eth.wait_for_transaction_receipt(txh_send_bnb, timeout=args.timeout)
                    if receipt.status != 1:
                        print(f"  ✗ Failed to send BNB: {txh_send_bnb.hex()}")
                        failed_count += 1
                        continue
                    time.sleep(0.5)  # Small delay
                else:
                    print(f"  → Gas txHash: {txh_send_bnb.hex()}")

                # Step 2: Claim USDT from faucet
                print(f"  → Claiming USDT from faucet...")
                new_nonce = 0
                raw_faucet = claim_usdt_from_faucet(w3, new_acct, usdt, faucet_bnb_wei, new_nonce)
                txh_faucet = w3.eth.send_raw_transaction(raw_faucet)
                new_nonce += 1
                if args.wait:
                    receipt = w3.eth.wait_for_transaction_receipt(txh_faucet, timeout=args.timeout)
                    if receipt.status != 1:
                        print(f"  ✗ Failed to claim USDT: {txh_faucet.hex()}")
                        failed_count += 1
                        continue
                    time.sleep(0.5)
                else:
                    print(f"  → Faucet txHash: {txh_faucet.hex()}")

                # Step 3: Transfer USDT to Controller
                print(f"  → Transferring {args.amount_usdt} USDT to Controller...")
                raw_transfer = transfer_usdt_to_controller(w3, new_acct, usdt_contract, controller, amount_usdt_wei, new_nonce)
                txh_transfer = w3.eth.send_raw_transaction(raw_transfer)
                if args.wait:
                    receipt = w3.eth.wait_for_transaction_receipt(txh_transfer, timeout=args.timeout)
                    if receipt.status != 1:
                        print(f"  ✗ Failed to transfer USDT: {txh_transfer.hex()}")
                        failed_count += 1
                        continue
                    time.sleep(0.5)
                else:
                    print(f"  → Transfer txHash: {txh_transfer.hex()}")

                print(f"  ✓ Success: {new_address}")
                success_count += 1

                # Small delay between addresses to avoid nonce issues
                if not args.wait and i < args.count:
                    time.sleep(0.2)

            except Exception as e:
                print(f"  ✗ Error processing {new_address}: {e}", file=sys.stderr)
                failed_count += 1
                continue

    # Save keys if requested (only in fixed count mode or if we collected accounts)
    if args.save_keys and not run_until_target:
        keys_data = [{"address": addr, "private_key": pk} for addr, pk in accounts]
        with open("batch_deposit_keys.json", "w") as f:
            json.dump(keys_data, f, indent=2)
        print(f"\nSaved keys to batch_deposit_keys.json")
    elif args.save_keys and run_until_target and all_accounts:
        keys_data = [{"address": addr, "private_key": pk} for addr, pk in all_accounts]
        with open("batch_deposit_keys.json", "w") as f:
            json.dump(keys_data, f, indent=2)
        print(f"\nSaved {len(all_accounts)} keys to batch_deposit_keys.json")

    # Final summary
    print(f"\n{'='*60}")
    print(f"Summary:")
    if run_until_target:
        print(f"  Mode: Auto (until target reached)")
        print(f"  Total addresses generated: {len(all_accounts) if all_accounts else success_count + failed_count}")
    else:
        print(f"  Mode: Fixed count")
        print(f"  Total addresses: {args.count}")
    print(f"  Success: {success_count}")
    print(f"  Failed: {failed_count}")

    if args.wait:
        time.sleep(2)
        controller_bal_after = usdt_contract.functions.balanceOf(controller).call()
        print(f"  Controller USDT balance after: {controller_bal_after / (10**args.decimals):.2f}")
        print(f"  Total deposited: {(controller_bal_after - controller_bal_before) / (10**args.decimals):.2f} USDT")

    if success_count > 0:
        print(f"\n✅ Batch deposit completed!")
    else:
        print(f"\n❌ All deposits failed!")
        return 1

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
