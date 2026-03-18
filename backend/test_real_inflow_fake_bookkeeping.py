from __future__ import annotations

import argparse
import os
import secrets
import time

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

USDT_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

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
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "keepers",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
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

def _norm_pk(raw: str) -> str:
    s = raw.strip()
    if s and not s.startswith("0x"):
        s = "0x" + s
    return s

def _send_tx(w3: Web3, account, tx: dict):
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        raise RuntimeError("signed tx missing raw bytes")
    return w3.eth.send_raw_transaction(raw)

def main() -> int:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(
        description="Real inflow + forged bookkeeping to TRANSFER_TAX_RECEIVER_C"
    )
    parser.add_argument(
        "--amount-u",
        type=float,
        default=float(os.getenv("MALICIOUS_REAL_INFLOW_U", "100")),
        help="Real inflow amount U (also used as forged bookkeeping amount).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.getenv("MALICIOUS_WAIT_SECONDS", "45")),
        help="Wait after malicious tx, then print keeperAccountingPaused.",
    )
    parser.add_argument(
        "--fake-tx-hash",
        default=os.getenv("MALICIOUS_TEST_TX_HASH", ""),
        help="Forged tx hash (optional). Auto-generated if empty.",
    )
    args = parser.parse_args()

    rpc = _pick_rpc_url()
    controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
    usdt_addr = os.getenv("USDT_ADDRESS", "").strip()
    target_user = os.getenv("TRANSFER_TAX_RECEIVER_C", "").strip()
    keeper_pk = _norm_pk(os.getenv("KEEPER_PRIVATE_KEY", ""))
    funder_pk = _norm_pk(os.getenv("MALICIOUS_TEST_FUNDER_PRIVATE_KEY", ""))

    if not rpc or not controller_addr or not usdt_addr or not target_user or not keeper_pk or not funder_pk:
        print("ERROR: missing env values.")
        print("Need: CONTROLLER_ADDRESS, USDT_ADDRESS, TRANSFER_TAX_RECEIVER_C, KEEPER_PRIVATE_KEY, MALICIOUS_TEST_FUNDER_PRIVATE_KEY, RPC")
        return 2

    w3 = Web3(Web3.HTTPProvider(rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"ERROR: RPC not connected: {rpc}")
        return 2

    controller_addr = Web3.to_checksum_address(controller_addr)
    usdt_addr = Web3.to_checksum_address(usdt_addr)
    target_user = Web3.to_checksum_address(target_user)
    keeper = Account.from_key(keeper_pk)
    funder = Account.from_key(funder_pk)

    usdt = w3.eth.contract(address=usdt_addr, abi=USDT_ABI)
    controller = w3.eth.contract(address=controller_addr, abi=CONTROLLER_ABI)

    try:
        decimals = int(usdt.functions.decimals().call())
    except Exception:
        decimals = 18
    amount = int(args.amount_u * (10**decimals))

    fake_tx_hash = args.fake_tx_hash.strip() or ("0x" + secrets.token_hex(32))
    if len(fake_tx_hash) != 66:
        print(f"ERROR: invalid fake tx hash: {fake_tx_hash}")
        return 2

    print("=== Real Inflow + Legacy Forged Bookkeeping Probe ===")
    print(f"rpc={rpc}")
    print(f"controller={controller_addr}")
    print(f"usdt={usdt_addr} decimals={decimals}")
    print(f"funder={funder.address}")
    print(f"keeper={keeper.address}")
    print(f"book_to_user(TRANSFER_TAX_RECEIVER_C)={target_user}")
    print(f"amount_u={args.amount_u}")
    print(f"fake_tx_hash={fake_tx_hash}")
    print("")

    is_keeper = bool(controller.functions.keepers(keeper.address).call())
    if not is_keeper:
        print("ERROR: KEEPER_PRIVATE_KEY address is not whitelisted keeper.")
        return 2

    funder_bal = int(usdt.functions.balanceOf(funder.address).call())
    if funder_bal < amount:
        print(f"ERROR: funder balance insufficient. balance={funder_bal}, need={amount}")
        return 2

    before_inflow = int(controller.functions.keeperAvailableUsdtInflow().call())
    before_paused = bool(controller.functions.keeperAccountingPaused().call())
    print(f"precheck: keeperAvailableUsdtInflow={before_inflow}")
    print(f"precheck: keeperAccountingPaused={before_paused}")

    print("\n[1/3] send real inflow to controller...")
    tx1_fn = usdt.functions.transfer(controller_addr, amount)
    nonce1 = w3.eth.get_transaction_count(funder.address)
    try:
        gas1 = int(tx1_fn.estimate_gas({"from": funder.address}) * 12 // 10)
    except Exception:
        gas1 = 120000
    tx1 = tx1_fn.build_transaction(
        {
            "from": funder.address,
            "nonce": nonce1,
            "gas": gas1,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    txh1 = _send_tx(w3, funder, tx1)
    rc1 = w3.eth.wait_for_transaction_receipt(txh1, timeout=180)
    print(f"real_inflow_tx={txh1.hex()} status={rc1.status} block={rc1.blockNumber}")
    if rc1.status != 1:
        print("ERROR: real inflow transfer failed.")
        return 2

    print("\n[2/3] keeper forged legacy bookkeeping to TRANSFER_TAX_RECEIVER_C...")
    fn = controller.functions.depositFromTransfer(target_user, amount, Web3.to_bytes(hexstr=fake_tx_hash))
    nonce2 = w3.eth.get_transaction_count(keeper.address)
    try:
        gas2 = int(fn.estimate_gas({"from": keeper.address}) * 12 // 10)
    except Exception:
        gas2 = 250000
    tx2 = fn.build_transaction(
        {
            "from": keeper.address,
            "nonce": nonce2,
            "gas": gas2,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    txh2 = _send_tx(w3, keeper, tx2)
    rc2 = w3.eth.wait_for_transaction_receipt(txh2, timeout=180)
    print(f"forged_book_tx={txh2.hex()} status={rc2.status} block={rc2.blockNumber}")
    if rc2.status == 1:
        print("ERROR: legacy bookkeeping tx unexpectedly succeeded.")
        return 2
    print("PASS: legacy bookkeeping tx reverted as expected (legacy entry disabled).")

    print(f"\n[3/3] wait {args.wait_seconds}s and check keeperAccountingPaused...")
    time.sleep(max(0, args.wait_seconds))
    after_inflow = int(controller.functions.keeperAvailableUsdtInflow().call())
    after_paused = bool(controller.functions.keeperAccountingPaused().call())
    print(f"postcheck: keeperAvailableUsdtInflow={after_inflow}")
    print(f"postcheck: keeperAccountingPaused={after_paused}")
    print("")
    print("Next checks:")
    print("- verify witness entry path is used in backend/listener")
    print("- keep validator alerting enabled for defense-in-depth")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
