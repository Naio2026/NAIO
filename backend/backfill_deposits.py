from __future__ import annotations

import argparse
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

def _get_log_level() -> int:
    s = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, s, logging.INFO)

logging.basicConfig(level=_get_log_level(), format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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
            raise RuntimeError("USE_QUICKNODE=true but QUICKNODE_HTTP_URL empty")
        return url
    url = os.getenv("BSC_RPC_URL", "").strip()
    if not url:
        url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    return url

TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
if isinstance(TRANSFER_TOPIC, str) and not TRANSFER_TOPIC.startswith("0x"):
    TRANSFER_TOPIC = "0x" + TRANSFER_TOPIC

def _pad_topic_address(addr: str) -> str:
    a = Web3.to_checksum_address(addr).lower().replace("0x", "")
    return "0x" + ("0" * 24) + a

CONTROLLER_ABI = [
    {
        "inputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "name": "processedTransfers",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

def _to_hex_str(x) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, (bytes, bytearray)):
        return "0x" + bytes(x).hex()
    if hasattr(x, "hex"):
        h = x.hex()
        if isinstance(h, str):
            return h if h.startswith("0x") else "0x" + h
    return str(x)

def _parse_transfer_log(log_obj) -> Optional[dict]:
    try:
        topics = log_obj.get("topics")
        if not topics or len(topics) < 3:
            return None
        t1 = _to_hex_str(topics[1])
        t2 = _to_hex_str(topics[2])
        from_addr = Web3.to_checksum_address("0x" + t1[-40:])
        to_addr = Web3.to_checksum_address("0x" + t2[-40:])
        data_hex = _to_hex_str(log_obj.get("data"))
        amount = int(data_hex, 16)
        tx_hash = _to_hex_str(log_obj.get("transactionHash"))
        bn = log_obj.get("blockNumber")
        block_number = int(bn, 16) if isinstance(bn, str) else int(bn)
        return {"from": from_addr, "to": to_addr, "amount": amount, "txHash": tx_hash, "blockNumber": block_number}
    except Exception:
        return None

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-block", required=True, help="start block number")
    ap.add_argument("--to-block", default="latest", help="end block number or 'latest'")
    ap.add_argument("--dry-run", action="store_true", help="scan only mode (required)")
    args = ap.parse_args()
    if not args.dry_run:
        raise RuntimeError("legacy on-chain backfill has been disabled; use --dry-run to scan candidates only")

    load_dotenv(override=True)

    controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
    usdt_addr = os.getenv("USDT_ADDRESS", "").strip()
    pool_seeder_addr = os.getenv("INITIAL_POOL_SEEDER_ADDRESS", "").strip()
    if not controller_addr or not usdt_addr:
        raise RuntimeError("Missing CONTROLLER_ADDRESS/USDT_ADDRESS in .env")

    controller = Web3.to_checksum_address(controller_addr)
    usdt = Web3.to_checksum_address(usdt_addr)
    pool_seeder = Web3.to_checksum_address(pool_seeder_addr) if pool_seeder_addr else None
    rpc = _pick_rpc_url()

    w3 = Web3(Web3.HTTPProvider(rpc))
    _inject_poa(w3)
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {rpc}")

    confirmations = int(os.getenv("CONFIRMATIONS", "3"))
    if confirmations < 0:
        confirmations = 0

    max_blocks_per_scan = int(os.getenv("MAX_BLOCKS_PER_SCAN", "2000"))
    log_chunk_size = int(os.getenv("LOG_CHUNK_SIZE", "200"))
    log_fetch_threads = int(os.getenv("LOG_FETCH_THREADS", "8"))

    from_block = int(args.from_block)
    if args.to_block.strip().lower() == "latest":
        head = w3.eth.block_number
        to_block = max(0, head - confirmations)
    else:
        to_block = int(args.to_block)

    logger.info("RPC: %s", rpc)
    logger.info("Controller: %s", controller)
    logger.info("USDT: %s", usdt)
    if pool_seeder:
        logger.info("InitialPoolSeeder: %s (skip seeder->controller transfers)", pool_seeder)
    logger.info("Scan range: %s -> %s (confirmations=%s)", from_block, to_block, confirmations)
    logger.info("dry_run=%s", args.dry_run)

    controller_topic = _pad_topic_address(controller)

    contract = w3.eth.contract(address=controller, abi=CONTROLLER_ABI)
    def get_logs_range(a: int, b: int):
        return w3.eth.get_logs(
            {"fromBlock": a, "toBlock": b, "address": usdt, "topics": [TRANSFER_TOPIC, None, controller_topic]}
        )

    def get_logs_parallel(a: int, b: int):
        if b < a:
            return []
        if (b - a + 1) <= log_chunk_size or log_fetch_threads <= 1:
            return get_logs_range(a, b)
        futures = []
        start = a
        with ThreadPoolExecutor(max_workers=max(1, log_fetch_threads)) as ex:
            while start <= b:
                end = min(b, start + log_chunk_size - 1)
                futures.append(ex.submit(get_logs_range, start, end))
                start = end + 1
            out = []
            for f in as_completed(futures):
                out.extend(f.result())
            return out

    processed = 0
    skipped = 0
    failed = 0

    cur = from_block
    while cur <= to_block:
        end = min(to_block, cur + max_blocks_per_scan)
        logger.info("Scanning blocks: %s -> %s", cur, end)
        logs = get_logs_parallel(cur, end)
        logs = sorted(logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))
        for lg in logs:
            t = _parse_transfer_log(lg)
            if not t:
                continue
            if t["from"].lower() == "0x0000000000000000000000000000000000000000":

                continue
            if pool_seeder and t["from"].lower() == pool_seeder.lower():

                skipped += 1
                logger.info("Skip seeder funding transfer: tx=%s amount=%s", t["txHash"], t["amount"])
                continue
            txh = t["txHash"]
            txh_bytes32 = Web3.to_bytes(hexstr=txh)
            if contract.functions.processedTransfers(txh_bytes32).call():
                skipped += 1
                continue
                processed += 1
            logger.info("[DRY] backfill candidate tx=%s user=%s amount=%s", txh, t["from"], t["amount"])

        cur = end + 1

    logger.info("Done. processed=%s skipped=%s failed=%s", processed, skipped, failed)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
