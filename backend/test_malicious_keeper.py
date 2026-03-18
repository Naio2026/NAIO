from __future__ import annotations

import argparse
import os
import secrets
import sys
import time

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

CONTROLLER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "uint256", "name": "usdtAmount", "type": "uint256"},
            {"internalType": "bytes32", "name": "txHash", "type": "bytes32"},
        ],
        "name": "depositFromTransfer",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "keeperAvailableUsdtInflow",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "keeperAccountingPaused",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "keepers",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _pick_rpc_url() -> str:
    if _env_bool("USE_QUICKNODE", False):
        url = os.getenv("QUICKNODE_HTTP_URL", "").strip()
        if url:
            return url
    return os.getenv("BSC_RPC_URL", "").strip() or os.getenv("BSC_TESTNET_RPC", "").strip()

def _extract_revert_msg(exc: Exception) -> str:
    msg = str(exc)
    marker = "execution reverted:"
    idx = msg.lower().find(marker)
    if idx >= 0:
        return msg[idx + len(marker):].strip()
    return msg.strip()

def main() -> int:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(
        description="Test malicious keeper scenario (forged legacy depositFromTransfer)."
    )
    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="Also send a real tx to prove on-chain revert (costs gas).",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("MALICIOUS_TEST_USER", "0x000000000000000000000000000000000000dEaD"),
        help="Fake user address passed to legacy depositFromTransfer.",
    )
    parser.add_argument(
        "--amount-usdt",
        type=float,
        default=float(os.getenv("MALICIOUS_TEST_AMOUNT_USDT", "123.45")),
        help="Forged USDT amount (18 decimals).",
    )
    parser.add_argument(
        "--tx-hash",
        default=os.getenv("MALICIOUS_TEST_TX_HASH", ""),
        help="Fake tx hash (0x...). Leave empty to auto-generate random bytes32.",
    )
    args = parser.parse_args()

    rpc = _pick_rpc_url()
    controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
    keeper_pk = os.getenv("KEEPER_PRIVATE_KEY", "").strip()

    if not rpc:
        print("ERROR: missing RPC URL (QUICKNODE_HTTP_URL / BSC_RPC_URL / BSC_TESTNET_RPC)")
        return 2
    if not controller_addr:
        print("ERROR: missing CONTROLLER_ADDRESS")
        return 2
    if not keeper_pk:
        print("ERROR: missing KEEPER_PRIVATE_KEY")
        return 2
    if not keeper_pk.startswith("0x"):
        keeper_pk = "0x" + keeper_pk

    w3 = Web3(Web3.HTTPProvider(rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"ERROR: RPC not connected: {rpc}")
        return 2

    keeper = Account.from_key(keeper_pk)
    controller = w3.eth.contract(address=Web3.to_checksum_address(controller_addr), abi=CONTROLLER_ABI)
    user = Web3.to_checksum_address(args.user)
    amount_wei = int(args.amount_usdt * 10**18)

    fake_tx_hash = args.tx_hash.strip()
    if not fake_tx_hash:
        fake_tx_hash = "0x" + secrets.token_hex(32)
    if len(fake_tx_hash) != 66 or not fake_tx_hash.startswith("0x"):
        print(f"ERROR: invalid tx hash format: {fake_tx_hash}")
        return 2

    print("=== Malicious Keeper Legacy-Entry Simulation ===")
    print(f"rpc={rpc}")
    print(f"controller={controller.address}")
    print(f"keeper={keeper.address}")
    print(f"user={user}")
    print(f"amount_wei={amount_wei}")
    print(f"fake_tx_hash={fake_tx_hash}")
    print(f"broadcast={args.broadcast}")
    print("")

    try:
        is_keeper = bool(controller.functions.keepers(keeper.address).call())
        paused = bool(controller.functions.keeperAccountingPaused().call())
        inflow = int(controller.functions.keeperAvailableUsdtInflow().call())
    except Exception as e:
        print(f"ERROR: pre-check failed: {e}")
        return 2

    print(f"precheck: keepers[{keeper.address}]={is_keeper}")
    print(f"precheck: keeperAccountingPaused={paused}")
    print(f"precheck: keeperAvailableUsdtInflow={inflow}")

    fn = controller.functions.depositFromTransfer(user, amount_wei, Web3.to_bytes(hexstr=fake_tx_hash))

    print("\n[1/2] eth_call dry-run...")
    try:
        fn.call({"from": keeper.address})
        print("ERROR: eth_call succeeded unexpectedly.")
        return 2
    except Exception as e:
        reason = _extract_revert_msg(e)
        print(f"PASS: forged call rejected, revert={reason}")

    if not args.broadcast:
        print("\nDone (dry-run only).")
        return 0

    print("\n[2/2] broadcast revert-check tx...")
    try:
        nonce = w3.eth.get_transaction_count(keeper.address)
        try:
            gas_est = fn.estimate_gas({"from": keeper.address})
            gas_limit = int(gas_est * 12 // 10)
        except Exception:
            gas_limit = 250000

        tx = fn.build_transaction(
            {
                "from": keeper.address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            }
        )
        signed = keeper.sign_transaction(tx)
        raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        if raw is None:
            print("ERROR: signed tx missing raw bytes")
            return 2

        sent = w3.eth.send_raw_transaction(raw)
        print(f"tx sent: {sent.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(sent, timeout=120)
        print(f"receipt.status={receipt.status} block={receipt.blockNumber}")
        if receipt.status == 0:
            print("PASS: on-chain tx reverted as expected (legacy entry blocked).")
        else:
            print("WARNING: tx succeeded; verify whether fresh inflow was available at that moment.")
    except Exception as e:
        print(f"ERROR: broadcast check failed: {e}")
        return 2

    print("\nDone.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
