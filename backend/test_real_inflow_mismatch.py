from __future__ import annotations

import argparse
import os
import secrets
import sys
from dataclasses import dataclass

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
        "inputs": [],
        "name": "keeperAvailableUsdtInflow",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
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

@dataclass
class Cfg:
    rpc: str
    usdt: str
    controller: str
    keeper_pk: str
    funder_pk: str
    real_inflow_u: float
    malicious_book_u: float

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

def _normalize_pk(raw: str) -> str:
    s = raw.strip()
    if s and not s.startswith("0x"):
        s = "0x" + s
    return s

def _load_cfg(args: argparse.Namespace) -> Cfg:
    keeper_pk = _normalize_pk(os.getenv("KEEPER_PRIVATE_KEY", ""))
    funder_pk = _normalize_pk(os.getenv("MALICIOUS_TEST_FUNDER_PRIVATE_KEY", ""))
    if not funder_pk:

        funder_pk = keeper_pk
    return Cfg(
        rpc=_pick_rpc_url(),
        usdt=os.getenv("USDT_ADDRESS", "").strip(),
        controller=os.getenv("CONTROLLER_ADDRESS", "").strip(),
        keeper_pk=keeper_pk,
        funder_pk=funder_pk,
        real_inflow_u=args.real_inflow_u,
        malicious_book_u=args.malicious_book_u,
    )

def _extract_revert_msg(exc: Exception) -> str:
    msg = str(exc)
    marker = "execution reverted:"
    idx = msg.lower().find(marker)
    if idx >= 0:
        return msg[idx + len(marker):].strip()
    return msg.strip()

def _send_tx(w3: Web3, account, tx: dict):
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        raise RuntimeError("signed tx missing raw bytes")
    txh = w3.eth.send_raw_transaction(raw)
    return txh

def main() -> int:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description="Real inflow + legacy malicious bookkeeping probe.")
    parser.add_argument("--real-inflow-u", type=float, default=float(os.getenv("MALICIOUS_REAL_INFLOW_U", "100")))
    parser.add_argument("--malicious-book-u", type=float, default=float(os.getenv("MALICIOUS_BOOK_U", "1000")))
    parser.add_argument("--fake-tx-hash", default=os.getenv("MALICIOUS_TEST_TX_HASH", ""))
    args = parser.parse_args()

    cfg = _load_cfg(args)
    if not cfg.rpc or not cfg.usdt or not cfg.controller or not cfg.keeper_pk or not cfg.funder_pk:
        print("ERROR: missing required env values.")
        print("Need: RPC, USDT_ADDRESS, CONTROLLER_ADDRESS, KEEPER_PRIVATE_KEY, MALICIOUS_TEST_FUNDER_PRIVATE_KEY(optional, fallback keeper)")
        return 2

    w3 = Web3(Web3.HTTPProvider(cfg.rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"ERROR: RPC not connected: {cfg.rpc}")
        return 2

    keeper = Account.from_key(cfg.keeper_pk)
    funder = Account.from_key(cfg.funder_pk)
    controller_addr = Web3.to_checksum_address(cfg.controller)
    usdt = w3.eth.contract(address=Web3.to_checksum_address(cfg.usdt), abi=USDT_ABI)
    controller = w3.eth.contract(address=controller_addr, abi=CONTROLLER_ABI)

    try:
        decimals = int(usdt.functions.decimals().call())
    except Exception:
        decimals = 18

    real_inflow_amt = int(cfg.real_inflow_u * (10**decimals))
    malicious_book_amt = int(cfg.malicious_book_u * (10**decimals))

    fake_tx_hash = args.fake_tx_hash.strip() or ("0x" + secrets.token_hex(32))
    if len(fake_tx_hash) != 66:
        print(f"ERROR: invalid fake tx hash: {fake_tx_hash}")
        return 2

    print("=== Target-2 Simulation ===")
    print(f"rpc={cfg.rpc}")
    print(f"controller={controller_addr}")
    print(f"usdt={cfg.usdt} decimals={decimals}")
    print(f"keeper={keeper.address}")
    print(f"funder={funder.address}")
    print(f"real_inflow_u={cfg.real_inflow_u}")
    print(f"malicious_book_u={cfg.malicious_book_u}")
    print(f"fake_tx_hash={fake_tx_hash}")
    print("")

    is_keeper = bool(controller.functions.keepers(keeper.address).call())
    if not is_keeper:
        print("ERROR: keeper address is not whitelisted in controller.")
        return 2

    funder_bal = int(usdt.functions.balanceOf(funder.address).call())
    print(f"precheck: funder_usdt_balance={funder_bal}")
    if funder_bal < real_inflow_amt:
        print("ERROR: funder USDT balance is insufficient for real inflow step.")
        return 2

    print("\n[1/3] send real inflow transfer to controller...")
    tx1_fn = usdt.functions.transfer(controller_addr, real_inflow_amt)
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
    print(f"real inflow tx sent: {txh1.hex()}")
    rc1 = w3.eth.wait_for_transaction_receipt(txh1, timeout=180)
    print(f"real inflow receipt.status={rc1.status} block={rc1.blockNumber}")
    if rc1.status != 1:
        print("ERROR: real inflow transfer failed.")
        return 2

    print("\n[2/3] dry-run legacy malicious bookkeeping...")
    fn_mal = controller.functions.depositFromTransfer(
        funder.address, malicious_book_amt, Web3.to_bytes(hexstr=fake_tx_hash)
    )
    try:
        fn_mal.call({"from": keeper.address})
        print("ERROR: dry-run unexpectedly succeeded.")
        return 2
    except Exception as e:
        reason = _extract_revert_msg(e)
        print(f"PASS: dry-run rejected, revert={reason}")

    print("\n[3/3] broadcast legacy malicious bookkeeping tx...")
    nonce3 = w3.eth.get_transaction_count(keeper.address)
    try:
        gas3 = int(fn_mal.estimate_gas({"from": keeper.address}) * 12 // 10)
    except Exception:
        gas3 = 250000
    tx3 = fn_mal.build_transaction(
        {
            "from": keeper.address,
            "nonce": nonce3,
            "gas": gas3,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    txh3 = _send_tx(w3, keeper, tx3)
    print(f"malicious tx sent: {txh3.hex()}")
    rc3 = w3.eth.wait_for_transaction_receipt(txh3, timeout=180)
    print(f"malicious receipt.status={rc3.status} block={rc3.blockNumber}")
    if rc3.status == 0:
        print("PASS: on-chain reverted as expected (legacy entry blocked).")
    else:
        print("WARNING: malicious tx succeeded; check if fresh inflow was unexpectedly large at execution time.")

    print("\nDone.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
