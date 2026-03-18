import argparse
import os
import time
import logging
import sqlite3
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
        url = "https://bsc-dataseed1.binance.org/"
    return url

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

DEPOSIT_TOPIC = Web3.keccak(text="Deposit(address,uint256,address,uint256)").hex()
REFERRAL_BOUND_TOPIC = Web3.keccak(text="ReferralBound(address,address)").hex()
if isinstance(DEPOSIT_TOPIC, str) and not DEPOSIT_TOPIC.startswith("0x"):
    DEPOSIT_TOPIC = "0x" + DEPOSIT_TOPIC
if isinstance(REFERRAL_BOUND_TOPIC, str) and not REFERRAL_BOUND_TOPIC.startswith("0x"):
    REFERRAL_BOUND_TOPIC = "0x" + REFERRAL_BOUND_TOPIC

def _init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    with conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS deposit_records ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_address TEXT NOT NULL, "
            "amount_wei TEXT NOT NULL, "
            "tx_hash TEXT NOT NULL UNIQUE, "
            "block_number INTEGER NOT NULL, "
            "block_timestamp INTEGER NOT NULL, "
            "referrer_address TEXT, "
            "power_added TEXT, "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_user ON deposit_records(user_address)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_tx ON deposit_records(tx_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_block ON deposit_records(block_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_ts ON deposit_records(block_timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_referrer ON deposit_records(referrer_address)")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS referral_relations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_address TEXT NOT NULL UNIQUE, "
            "referrer_address TEXT NOT NULL, "
            "block_number INTEGER NOT NULL, "
            "block_timestamp INTEGER NOT NULL, "
            "tx_hash TEXT NOT NULL, "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_relations_user ON referral_relations(user_address)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_relations_referrer ON referral_relations(referrer_address)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_relations_tx ON referral_relations(tx_hash)")
    conn.close()

def _store_deposit(db_path: str, user: str, amount: int, tx_hash: str, block_number: int,
                   block_ts: int, referrer: Optional[str] = None, power: Optional[int] = None) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO deposit_records "
                "(user_address, amount_wei, tx_hash, block_number, block_timestamp, "
                "referrer_address, power_added, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user.lower(), str(amount), tx_hash.lower(), block_number, block_ts,
                 referrer.lower() if referrer and referrer != "0x0000000000000000000000000000000000000000" else None,
                 str(power) if power is not None else None, int(time.time()))
            )
        conn.close()
    except Exception as e:
        logger.error("Failed to store deposit record: %s", e)

def _store_referral(db_path: str, user: str, referrer: str, block_number: int,
                    block_ts: int, tx_hash: str) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO referral_relations "
                "(user_address, referrer_address, block_number, block_timestamp, tx_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user.lower(), referrer.lower(), block_number, block_ts, tx_hash.lower(), int(time.time()))
            )
        conn.close()
    except Exception as e:
        logger.error("Failed to store referral relation: %s", e)

def _parse_deposit_event(log_obj, block_ts: int) -> Optional[dict]:
    try:
        topics = log_obj.get("topics")

        if not topics or len(topics) < 3:
            return None

        user = Web3.to_checksum_address("0x" + _to_hex_str(topics[1])[-40:])
        referrer = Web3.to_checksum_address("0x" + _to_hex_str(topics[2])[-40:])
        data_hex = _to_hex_str(log_obj.get("data"))

        if len(data_hex) < 130:
            return None

        amount = int(data_hex[2:66], 16)
        power = int(data_hex[66:130], 16)

        tx_hash = _to_hex_str(log_obj.get("transactionHash"))
        bn = log_obj.get("blockNumber")
        block_number = int(bn, 16) if isinstance(bn, str) else int(bn)

        return {
            "user": user,
            "amount": amount,
            "referrer": referrer if referrer != "0x0000000000000000000000000000000000000000" else None,
            "power": power,
            "tx_hash": tx_hash,
            "block_number": block_number,
            "block_timestamp": block_ts
        }
    except Exception as e:
        logger.debug("Failed to parse Deposit event: %s", e)
        return None

def _parse_referral_bound_event(log_obj, block_ts: int) -> Optional[dict]:
    try:
        topics = log_obj.get("topics")
        if not topics or len(topics) < 3:
            return None

        user = Web3.to_checksum_address("0x" + _to_hex_str(topics[1])[-40:])
        inviter = Web3.to_checksum_address("0x" + _to_hex_str(topics[2])[-40:])

        tx_hash = _to_hex_str(log_obj.get("transactionHash"))
        bn = log_obj.get("blockNumber")
        block_number = int(bn, 16) if isinstance(bn, str) else int(bn)

        return {
            "user": user,
            "inviter": inviter,
            "tx_hash": tx_hash,
            "block_number": block_number,
            "block_timestamp": block_ts
        }
    except Exception as e:
        logger.debug("Failed to parse ReferralBound event: %s", e)
        return None

def main() -> int:
    ap = argparse.ArgumentParser(description="追溯历史数据：补齐入金明细和推荐关系")
    ap.add_argument("--from-block", required=True, type=int, help="起始区块号")
    ap.add_argument("--to-block", default="latest", help="结束区块号或 'latest'")
    ap.add_argument("--dry-run", action="store_true", help="仅扫描，不存储")
    ap.add_argument(
        "--mode",
        default="all",
        choices=["all", "deposits-only", "referrals-only"],
        help="控制回填内容: all=入金明细+推荐关系, deposits-only=仅回填入金明细, referrals-only=仅回填推荐关系",
    )
    args = ap.parse_args()

    load_dotenv(override=True)

    controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
    if not controller_addr:
        raise RuntimeError("Missing CONTROLLER_ADDRESS in .env")

    controller = Web3.to_checksum_address(controller_addr)
    rpc = _pick_rpc_url()
    db_path = os.getenv("PRICE_DB_PATH", "price_history.db").strip() or "price_history.db"

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
    logger.info("DB: %s", db_path)
    logger.info("Scan range: %s -> %s (confirmations=%s)", from_block, to_block, confirmations)
    logger.info("dry_run=%s", args.dry_run)

    _init_db(db_path)

    def get_block_ts(block_number: int) -> int:
        try:
            return int(w3.eth.get_block(block_number)["timestamp"])
        except Exception:
            return int(time.time())

    def get_logs_range(a: int, b: int, topic: str):
        return w3.eth.get_logs(
            {"fromBlock": a, "toBlock": b, "address": controller, "topics": [topic]}
        )

    def get_logs_parallel(a: int, b: int, topic: str):
        if b < a:
            return []
        if (b - a + 1) <= log_chunk_size or log_fetch_threads <= 1:
            return get_logs_range(a, b, topic)
        futures = []
        start = a
        with ThreadPoolExecutor(max_workers=max(1, log_fetch_threads)) as ex:
            while start <= b:
                end = min(b, start + log_chunk_size - 1)
                futures.append(ex.submit(get_logs_range, start, end, topic))
                start = end + 1
            out = []
            for f in as_completed(futures):
                out.extend(f.result())
            return out

    deposits_count = 0
    referrals_count = 0
    skipped_deposits = 0
    skipped_referrals = 0

    cur = from_block
    while cur <= to_block:
        end = min(to_block, cur + max_blocks_per_scan)
        logger.info("Scanning blocks: %s -> %s", cur, end)

        if args.mode in ("all", "deposits-only"):
            deposit_logs = get_logs_parallel(cur, end, DEPOSIT_TOPIC)
            deposit_logs = sorted(deposit_logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))

            for lg in deposit_logs:
                bn = lg.get("blockNumber")
                block_number = int(bn, 16) if isinstance(bn, str) else int(bn)
                block_ts = get_block_ts(block_number)
                deposit = _parse_deposit_event(lg, block_ts)
                if not deposit:
                    continue

                if args.dry_run:
                    deposits_count += 1
                    logger.info(
                        "[DRY] Deposit: tx=%s user=%s amount=%s referrer=%s power=%s",
                        deposit["tx_hash"], deposit["user"], deposit["amount"] / 1e18,
                        deposit["referrer"], deposit["power"]
                    )
                else:
                    try:
                        _store_deposit(
                            db_path, deposit["user"], deposit["amount"], deposit["tx_hash"],
                            deposit["block_number"], deposit["block_timestamp"],
                            deposit["referrer"], deposit["power"]
                        )
                        deposits_count += 1
                    except Exception as e:
                        skipped_deposits += 1
                        logger.debug("Skipped deposit record (likely already exists): %s", e)

        if args.mode in ("all", "referrals-only"):
            referral_logs = get_logs_parallel(cur, end, REFERRAL_BOUND_TOPIC)
            referral_logs = sorted(referral_logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))

            for lg in referral_logs:
                bn = lg.get("blockNumber")
                block_number = int(bn, 16) if isinstance(bn, str) else int(bn)
                block_ts = get_block_ts(block_number)
                referral = _parse_referral_bound_event(lg, block_ts)
                if not referral:
                    continue

                if args.dry_run:
                    referrals_count += 1
                    logger.info(
                        "[DRY] ReferralBound: tx=%s user=%s inviter=%s",
                        referral["tx_hash"], referral["user"], referral["inviter"]
                    )
                else:
                    try:
                        _store_referral(
                            db_path, referral["user"], referral["inviter"],
                            referral["block_number"], referral["block_timestamp"], referral["tx_hash"]
                        )
                        referrals_count += 1
                    except Exception as e:
                        skipped_referrals += 1
                        logger.debug(f"跳过推荐关系（可能已存在）: {e}")

        cur = end + 1

    logger.info("完成。入金记录: %s (跳过: %s), 推荐关系: %s (跳过: %s)",
                deposits_count, skipped_deposits, referrals_count, skipped_referrals)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
