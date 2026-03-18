import importlib
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
import web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.providers.base import BaseProvider

def _get_websocket_provider_class():
    candidates = [
        ("web3.providers.legacy_websocket", "LegacyWebSocketProvider"),
        ("web3.providers.legacy_websocket", "LegacyWebsocketProvider"),
        ("web3.providers.websocket", "WebsocketProvider"),
        ("web3.providers.websocket", "WebSocketProvider"),
    ]
    for mod, attr in candidates:
        try:
            m = importlib.import_module(mod)
            cls = getattr(m, attr, None)
            if cls is not None and isinstance(cls, type) and issubclass(cls, BaseProvider):
                return cls
        except Exception:
            continue
    return None

def _inject_poa_middleware(w3: Web3) -> None:

    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

load_dotenv(override=True)

def _get_log_level() -> int:
    s = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, s, logging.INFO)

logging.basicConfig(
    level=_get_log_level(),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class PokeCaller:

    def __init__(self):
        logger.info(f"web3.py version: {web3.__version__}")

        use_quicknode = os.getenv("USE_QUICKNODE", "false").lower() == "true"

        use_websocket = os.getenv("USE_WEBSOCKET", "true").lower() == "true"
        ws_url = os.getenv("QUICKNODE_WS_URL", "") if use_quicknode else os.getenv("BSC_WS_URL", "")

        self.w3 = None
        if use_websocket and ws_url:
            ws_provider_cls = _get_websocket_provider_class()
            if ws_provider_cls is not None:
                try:
                    logger.info("Trying WebSocket RPC: %s", ws_url)
                    w3 = Web3(ws_provider_cls(ws_url))
                    _inject_poa_middleware(w3)
                    if not w3.is_connected():
                        raise Exception("WebSocket connection failed")
                    self.w3 = w3
                    logger.info("WebSocket connected: %s", ws_url)
                except Exception as e:
                    logger.warning("WebSocket failed: %s; falling back to HTTP", e)
                    self.w3 = None
            else:
                logger.warning("No compatible sync WebSocket provider in this web3.py version; using HTTP")
                self.w3 = None

        if self.w3 is None:
            if use_quicknode:
                rpc_url = os.getenv("QUICKNODE_HTTP_URL", "")
                if not rpc_url:
                    raise Exception("Please set QUICKNODE_HTTP_URL")
            else:
                rpc_url = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")

            self.w3 = Web3(Web3.HTTPProvider(rpc_url))
            _inject_poa_middleware(self.w3)
            logger.info("Connected to BSC RPC (HTTP): %s", rpc_url)

        if not self.w3.is_connected():
            raise Exception("Failed to connect to BSC RPC")

        controller_address = os.getenv('CONTROLLER_ADDRESS', '')
        if not controller_address:
            raise Exception("Please set CONTROLLER_ADDRESS")

        self.controller_address = Web3.to_checksum_address(controller_address)

        self.controller_abi = [
            {
                "inputs": [],
                "name": "poke",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "systemStartTs",
                "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "lastPokeEpoch",
                "outputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "totalPower",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            },
        ]

        self.controller_contract = self.w3.eth.contract(
            address=self.controller_address,
            abi=self.controller_abi
        )

        keeper_key = os.getenv("KEEPER_PRIVATE_KEY", "")
        if keeper_key:
            if not keeper_key.startswith("0x"):
                keeper_key = "0x" + keeper_key
            self.keeper_account = Account.from_key(keeper_key)
            self.keeper_address = self.keeper_account.address
            logger.info("Keeper address: %s", self.keeper_address)
        else:
            self.keeper_account = None
            logger.warning("KEEPER_PRIVATE_KEY not set; call_poke cannot send transactions automatically (manual trigger only)")

        self.epoch_seconds = int(os.getenv("EPOCH_SECONDS", "86400"))
        if self.epoch_seconds < 60:
            self.epoch_seconds = 86400

        self.poke_window_seconds = int(os.getenv("POKE_WINDOW_SECONDS", "360"))

        self.poke_gas_price_gwei = os.getenv("POKE_GAS_PRICE_GWEI", "").strip()
        self.poke_gas_price_gwei = float(self.poke_gas_price_gwei) if self.poke_gas_price_gwei else None

        state_path = os.getenv("POKE_STATE_FILE", "call_poke_state.json").strip() or "call_poke_state.json"
        self.state_file = Path(state_path) if os.path.isabs(state_path) else Path(__file__).resolve().parent / state_path

    def _load_state(self) -> dict:
        try:
            if self.state_file.exists():
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("Failed to read deflation state file: %s", e)
        return {"project_started": False}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to write deflation state file: %s", e)

    def _is_project_started(self) -> bool:
        try:
            start_ts = self.controller_contract.functions.systemStartTs().call()
            power = self.controller_contract.functions.totalPower().call()
            return (start_ts and int(start_ts) > 0) and (power and int(power) > 0)
        except Exception as e:
            logger.debug("Failed to check whether project started: %s", e)
            return False

    def _get_next_poke_ts(self) -> int | None:
        try:
            start_ts = self.controller_contract.functions.systemStartTs().call()
            last_epoch = self.controller_contract.functions.lastPokeEpoch().call()
            if not start_ts or int(start_ts) == 0:
                return None
            start_ts = int(start_ts)
            last_epoch = int(last_epoch)

            if last_epoch == 4294967295:
                last_epoch = -1
            return start_ts + (last_epoch + 2) * self.epoch_seconds
        except Exception as e:
            logger.debug("Failed to get next deflation timestamp: %s", e)
            return None

    def _gas_price_for_poke(self, multiplier: float = 1.0) -> int:
        base = self.w3.eth.gas_price

        gas_price = int(base * 1.2 * multiplier)
        if self.poke_gas_price_gwei is not None and self.poke_gas_price_gwei > 0:
            min_wei = int(self.poke_gas_price_gwei * 1e9)
            gas_price = max(gas_price, min_wei)
        return gas_price

    _POKE_REVERT_SELECTORS = {
        "0x6f312cbd": "NotStarted",
        "0x9488aaa6": "NotReady",
        "0x4b4edc2b": "AlreadyPokedToday",
        "0x5b7ab917": "NoDeflation",
    }

    def _parse_poke_revert(self, e: Exception) -> str | None:
        err_str = str(e)
        err_lower = err_str.lower()
        for sel, name in self._POKE_REVERT_SELECTORS.items():
            if sel in err_str or sel[2:] in err_lower:
                return name
        return None

    def can_poke(self) -> bool:
        try:
            if not self.keeper_account:
                return False
            self.controller_contract.functions.poke().estimate_gas({"from": self.keeper_address})
            return True
        except Exception as e:
            revert_name = self._parse_poke_revert(e)
            if revert_name is not None:
                logger.debug("Cannot poke now (contract revert: %s)", revert_name)
                return False
            logger.error("Failed to check poke status: %s", e)
            return False

    def call_poke(self) -> bool:
        if not self.keeper_account:
            logger.warning("未配置keeper私钥，无法自动调用poke")
            logger.info("提示：用户可以转 0.0003 BNB 到合约地址触发poke")
            return False

        try:
            function = self.controller_contract.functions.poke()
            try:
                gas_estimate = function.estimate_gas({"from": self.keeper_address})
                gas_limit = int(gas_estimate * 1.2)
            except Exception as e:
                logger.warning("Gas估算失败，使用默认值: %s", e)
                gas_limit = 500000

            nonce = self.w3.eth.get_transaction_count(self.keeper_address)
            last_error = None
            for attempt in range(3):

                mult = 1.0 + attempt * 0.5
                gas_price = self._gas_price_for_poke(multiplier=mult)
                try:
                    transaction = function.build_transaction({
                        "from": self.keeper_address,
                        "gas": gas_limit,
                        "gasPrice": gas_price,
                        "nonce": nonce,
                        "chainId": self.w3.eth.chain_id,
                    })
                    signed_txn = self.keeper_account.sign_transaction(transaction)
                    raw = getattr(signed_txn, "rawTransaction", None) or getattr(signed_txn, "raw_transaction", None)
                    if raw is None:
                        raise RuntimeError("SignedTransaction missing raw tx bytes (rawTransaction/raw_transaction)")
                    tx_hash = self.w3.eth.send_raw_transaction(raw)
                    logger.info("已发送poke交易: %s (gasPrice=%s wei)", tx_hash.hex(), gas_price)
                    receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                    if receipt.status == 1:
                        logger.info("poke执行成功，区块: %s", receipt.blockNumber)
                        return True
                    logger.error("poke交易失败: %s", tx_hash.hex())
                    return False
                except Exception as e:
                    last_error = e
                    err_msg = str(e).lower() if e else ""
                    if "replacement transaction underpriced" in err_msg or "underpriced" in err_msg:
                        logger.warning("第 %s 次发送被拒(underpriced)，加价重试: %s", attempt + 1, e)
                        continue
                    raise
            logger.error("调用poke失败(含重试): %s", last_error)
            return False
        except Exception as e:
            logger.error("调用poke失败: %s", e)
            return False

    def run(self, check_interval: int = 180):
        logger.info("开始通缩定时任务（仅在时间窗口内查链）")
        logger.info("检查间隔: %s 秒（%s 分钟），时间窗口: 前后 %s 秒", check_interval, check_interval // 60, self.poke_window_seconds)

        while True:
            try:
                state = self._load_state()
                project_started = state.get("project_started", False)

                if not project_started:
                    if self._is_project_started():
                        state["project_started"] = True
                        self._save_state(state)
                        logger.info("检测到首次入金，已启动通缩执行逻辑")
                        project_started = True
                    else:
                        logger.info("项目未启动（尚无入金），%s 秒后再次检查", check_interval)
                        time.sleep(check_interval)
                        continue

                next_ts = self._get_next_poke_ts()
                if next_ts is None:
                    logger.info("无法获取下一期通缩时间，%s 秒后重试", check_interval)
                    time.sleep(check_interval)
                    continue

                now = int(time.time())
                window_start = next_ts - self.poke_window_seconds
                window_end = next_ts + self.poke_window_seconds

                if now < window_start:
                    sleep_sec = min(check_interval, window_start - now)
                    logger.info("未到时间窗口（距窗口 %s 秒），休眠 %s 秒后复检", window_start - now, sleep_sec)
                    time.sleep(sleep_sec)
                    continue

                if now > window_end:
                    logger.info("已过时间窗口，%s 秒后重新拉取下一期时间", check_interval)
                    time.sleep(check_interval)
                    continue

                if self.can_poke():
                    logger.info("检测到可以执行通缩，开始调用 poke()...")
                    if self.call_poke():
                        logger.info("通缩执行成功")
                    else:
                        logger.error("通缩执行失败")
                else:
                    logger.info("当前不满足 poke 条件（窗口内，可能已 poke 或 epoch 未到），%s 秒后再查", check_interval)

                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("定时任务已停止")
                break
            except Exception as e:
                logger.error("定时任务出错: %s", e)
                time.sleep(check_interval)

def main():

    retry_delay = int(os.getenv("POKE_RETRY_DELAY", "10"))

    check_interval = int(os.getenv("POKE_CHECK_INTERVAL", "180"))
    while True:
        try:
            load_dotenv(override=True)
            caller = PokeCaller()
            caller.run(check_interval=check_interval)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"初始化/运行失败，将在 {retry_delay}s 后重试: {e}")
            time.sleep(retry_delay)

if __name__ == "__main__":
    main()
