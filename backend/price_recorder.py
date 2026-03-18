from __future__ import annotations

import os
import time
import logging
import sqlite3

from dotenv import load_dotenv
from web3 import Web3
import web3
from web3.middleware import ExtraDataToPOAMiddleware

def _get_log_level() -> int:
    s = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, s, logging.INFO)

logging.basicConfig(
    level=_get_log_level(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

def _inject_poa_middleware(w3: Web3) -> None:
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
    url = os.getenv("BSC_RPC_URL", "").strip()
    if not url:
        url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    return url

CONTROLLER_ABI_MIN = [
    {
        "inputs": [],
        "name": "getPrice",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

class PriceRecorder:
    def __init__(self) -> None:
        load_dotenv(override=True)
        logger.info("web3.py version: %s", web3.__version__)

        controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
        if not controller_addr:
            raise RuntimeError("Missing CONTROLLER_ADDRESS in .env")
        self.controller = Web3.to_checksum_address(controller_addr)

        rpc_url = _pick_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        _inject_poa_middleware(self.w3)
        if not self.w3.is_connected():
            raise RuntimeError(f"RPC not connected: {rpc_url}")

        self.controller_contract = self.w3.eth.contract(address=self.controller, abi=CONTROLLER_ABI_MIN)

        self.price_db_path = os.getenv("PRICE_DB_PATH", "price_history.db").strip() or "price_history.db"
        self.price_retention_days = int(os.getenv("PRICE_RETENTION_DAYS", "7"))
        self.price_record_interval = max(1, int(os.getenv("PRICE_RECORD_INTERVAL", "1")))

        self._init_price_db()
        logger.info("Price DB: %s interval=%ss retention=%sd", self.price_db_path, self.price_record_interval, self.price_retention_days)

    def _init_price_db(self) -> None:
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS price_points ("
                "ts INTEGER PRIMARY KEY, "
                "price_wei INTEGER NOT NULL)"
            )
        conn.close()

    def _purge_old_points(self, conn: sqlite3.Connection, now_ts: int) -> None:
        if self.price_retention_days <= 0:
            return
        cutoff = now_ts - (self.price_retention_days * 86400)
        conn.execute("DELETE FROM price_points WHERE ts < ?", (cutoff,))

    def record_price_point(self) -> None:
        ts = int(time.time())
        price = int(self.controller_contract.functions.getPrice().call())
        if price <= 0:
            logger.debug("price=0, skip recording")
            return
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO price_points(ts, price_wei) VALUES(?, ?)",
                (ts, price),
            )
            if ts % 3600 == 0:
                self._purge_old_points(conn, ts)
        conn.close()

    def run(self) -> None:
        while True:
            try:
                self.record_price_point()
            except Exception as e:
                logger.warning("record price failed: %s", e)
            time.sleep(self.price_record_interval)

def main() -> None:
    retry_delay = int(os.getenv("PRICE_RECORDER_RETRY_DELAY", "10"))
    while True:
        try:
            recorder = PriceRecorder()
            recorder.run()
        except KeyboardInterrupt:
            logger.info("Price recorder stopped")
            break
        except Exception as e:
            logger.error("Price recorder init/run failed, retry in %ss: %s", retry_delay, e)
            time.sleep(retry_delay)

if __name__ == "__main__":
    main()
