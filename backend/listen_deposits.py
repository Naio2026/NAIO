import os
import time
import json
import asyncio
import logging
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from web3 import Web3
import web3
from eth_account import Account
from eth_account.messages import encode_defunct

from web3.middleware import ExtraDataToPOAMiddleware
import importlib
from web3.providers.base import BaseProvider
import websockets
from eth_abi import encode as abi_encode

def _get_websocket_provider_cls():
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

WITNESS_TYPEHASH = Web3.keccak(
    text="NAIOWitness(uint256 chainId,address controller,address user,uint256 usdtAmount,bytes32 txHash,uint256 witnessDeadline)"
)

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_N_HALF = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0

class DepositListener:

    TRANSFER_EVENT_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

    if isinstance(TRANSFER_EVENT_TOPIC, str) and not TRANSFER_EVENT_TOPIC.startswith("0x"):
        TRANSFER_EVENT_TOPIC = "0x" + TRANSFER_EVENT_TOPIC

    DEPOSIT_EVENT_TOPIC = Web3.keccak(text="DepositFromTransfer(address,uint256,bytes32)").hex()
    REFUND_EVENT_TOPIC = Web3.keccak(text="DepositRefunded(address,uint256,bytes32,uint8)").hex()
    REFERRAL_BOUND_TOPIC = Web3.keccak(text="ReferralBound(address,address)").hex()
    DEPOSIT_DETAIL_TOPIC = Web3.keccak(text="Deposit(address,uint256,address,uint256)").hex()
    if isinstance(DEPOSIT_EVENT_TOPIC, str) and not DEPOSIT_EVENT_TOPIC.startswith("0x"):
        DEPOSIT_EVENT_TOPIC = "0x" + DEPOSIT_EVENT_TOPIC
    if isinstance(REFUND_EVENT_TOPIC, str) and not REFUND_EVENT_TOPIC.startswith("0x"):
        REFUND_EVENT_TOPIC = "0x" + REFUND_EVENT_TOPIC
    if isinstance(REFERRAL_BOUND_TOPIC, str) and not REFERRAL_BOUND_TOPIC.startswith("0x"):
        REFERRAL_BOUND_TOPIC = "0x" + REFERRAL_BOUND_TOPIC
    if isinstance(DEPOSIT_DETAIL_TOPIC, str) and not DEPOSIT_DETAIL_TOPIC.startswith("0x"):
        DEPOSIT_DETAIL_TOPIC = "0x" + DEPOSIT_DETAIL_TOPIC

    def __init__(self, config: dict):
        self.config = config
        logger.info(f"web3.py version: {web3.__version__}")

        self.w3 = Web3(Web3.HTTPProvider(config['rpc_url']))
        _inject_poa_middleware(self.w3)
        if not self.w3.is_connected():
            raise Exception("无法连接到BSC节点 (HTTP)")
        logger.info(f"已连接到BSC节点 (HTTP): {config['rpc_url']}")

        self.use_websocket = config.get('use_websocket', False)
        self.ws_url = config.get('ws_url') or ""
        if self.use_websocket and self.ws_url:
            logger.info(f"将使用WebSocket订阅(优先): {self.ws_url}")
        else:
            logger.info("未启用WebSocket订阅（未配置 ws_url 或 USE_WEBSOCKET=false），将仅使用HTTP轮询")

        self.usdt_address = Web3.to_checksum_address(config['usdt_address'])
        self.controller_address = Web3.to_checksum_address(config['controller_address'])
        self.pool_seeder_address = (
            Web3.to_checksum_address(config['pool_seeder_address'])
            if config.get('pool_seeder_address')
            else None
        )

        if config.get('keeper_private_key'):
            keeper_key = config["keeper_private_key"]
            if isinstance(keeper_key, str) and not keeper_key.startswith("0x"):
                keeper_key = "0x" + keeper_key
            self.keeper_account = Account.from_key(keeper_key)
            self.keeper_address = self.keeper_account.address
            logger.info(f"Keeper地址: {self.keeper_address}")
        else:
            self.keeper_account = None
            logger.warning("未配置keeper_private_key，将仅监听不调用合约")

        self.max_blocks_per_scan = int(os.getenv('MAX_BLOCKS_PER_SCAN', '2000'))
        self.log_chunk_size = int(os.getenv('LOG_CHUNK_SIZE', '200'))
        self.log_fetch_threads = int(os.getenv('LOG_FETCH_THREADS', '8'))
        self.tx_send_threads = int(os.getenv('TX_SEND_THREADS', '2'))
        self.max_inflight_txs = int(os.getenv('MAX_INFLIGHT_TXS', '200'))
        self.wait_for_receipt = os.getenv('WAIT_FOR_RECEIPT', 'true').lower() == 'true'
        self.receipt_timeout = int(os.getenv('RECEIPT_TIMEOUT', '120'))

        self.retry_gas_price_bump_bps = max(10000, int(os.getenv("RETRY_GAS_PRICE_BUMP_BPS", "12000")))

        self.tx_replace_interval_seconds = max(10, int(os.getenv("TX_REPLACE_INTERVAL_SECONDS", "60")))
        self.tx_max_replace_attempts = max(1, int(os.getenv("TX_MAX_REPLACE_ATTEMPTS", "8")))
        self.tx_max_bump_bps = max(
            self.retry_gas_price_bump_bps, int(os.getenv("TX_MAX_BUMP_BPS", "30000"))
        )

        self.use_subscription = os.getenv('USE_SUBSCRIPTION', 'true').lower() == 'true'

        self.subscription_reconnect_delay = int(os.getenv('SUBSCRIPTION_RECONNECT_DELAY', '3'))

        self.ws_ping_interval = int(os.getenv("WS_PING_INTERVAL", "20"))
        self.ws_ping_timeout = int(os.getenv("WS_PING_TIMEOUT", "20"))

        self.confirmations = int(os.getenv('CONFIRMATIONS', '3'))
        if self.confirmations < 0:
            self.confirmations = 0

        self._log_executor = ThreadPoolExecutor(max_workers=max(1, self.log_fetch_threads))
        self._tx_executor = ThreadPoolExecutor(max_workers=max(1, self.tx_send_threads))

        self._nonce_lock = threading.Lock()
        self._next_nonce: Optional[int] = None
        self._inflight_sem = threading.BoundedSemaphore(value=max(1, self.max_inflight_txs))

        self.price_db_path = os.getenv("PRICE_DB_PATH", "price_history.db").strip() or "price_history.db"
        self.price_retention_days = int(os.getenv("PRICE_RETENTION_DAYS", "7"))
        self._block_ts_cache: dict[int, int] = {}
        self._init_stats_db()

        self.start_block = config.get('start_block', 'latest')
        if self.start_block == 'latest':
            self.current_block = self.w3.eth.block_number
        else:
            self.current_block = int(self.start_block)

        logger.info(f"监听起始区块: {self.current_block}")
        if self.pool_seeder_address:
            logger.info(f"初始池子注资中继地址: {self.pool_seeder_address}（纯用户触发，无 keeper 处理）")

        self.controller_abi = [
            {
                "inputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
                "name": "processedTransfers",
                "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "address", "name": "user", "type": "address"},
                    {"internalType": "uint256", "name": "usdtAmount", "type": "uint256"},
                    {"internalType": "bytes32", "name": "txHash", "type": "bytes32"},
                    {"internalType": "uint256", "name": "witnessDeadline", "type": "uint256"},
                    {"internalType": "bytes[]", "name": "signatures", "type": "bytes[]"}
                ],
                "name": "depositFromTransferWitness",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            }
        ]
        self.controller_contract = self.w3.eth.contract(
            address=self.controller_address,
            abi=self.controller_abi
        )

        self.seeder_contract = None

        self.witness_signers = self._parse_witness_signers()
        self.witness_threshold = 3
        self.witness_deadline_seconds = max(30, int(os.getenv("WITNESS_SIGNATURE_DEADLINE_SECONDS", "600")))
        self.witness_deadline_refresh_margin_seconds = max(
            5, int(os.getenv("WITNESS_DEADLINE_REFRESH_MARGIN_SECONDS", "120"))
        )
        self.witness_auto_requeue_expired = os.getenv("WITNESS_AUTO_REQUEUE_EXPIRED", "true").strip().lower() == "true"
        self.witness_enabled = len(self.witness_signers) == self.witness_threshold
        if self.witness_signers and not self.witness_enabled:
            logger.warning(
                "3/3见证地址数量不正确：signers=%s required=%s",
                len(self.witness_signers),
                self.witness_threshold,
            )
        self.witness_hub_enable = self.witness_enabled and (os.getenv("WITNESS_HUB_ENABLE", "true").strip().lower() == "true")
        self.witness_hub_host = os.getenv("WITNESS_HUB_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.witness_hub_port = int(os.getenv("WITNESS_HUB_PORT", "8787"))
        self.witness_hub_api_key = os.getenv("WITNESS_HUB_API_KEY", "").strip()
        self.witness_worker_interval = max(1, int(os.getenv("WITNESS_WORKER_INTERVAL_SECONDS", "2")))
        self.witness_task_retention_seconds = max(60, int(os.getenv("WITNESS_TASK_RETENTION_SECONDS", "7200")))
        self._witness_lock = threading.Lock()
        self._witness_tasks: dict[str, dict] = {}
        self._witness_worker_stop = False
        self._witness_disabled_warned = False
        self._witness_hub_server = None
        self._witness_hub_thread = None
        self._witness_worker_thread = None
        if self.witness_enabled:
            if not self.keeper_account:
                logger.warning("见证模式需要 KEEPER_PRIVATE_KEY 进行链上提交；当前仅收集签名不提交")
            logger.info(
                "见证签名模式已启用：signers=%s threshold=%s deadline=%ss refresh_margin=%ss auto_requeue_expired=%s",
                len(self.witness_signers),
                self.witness_threshold,
                self.witness_deadline_seconds,
                self.witness_deadline_refresh_margin_seconds,
                self.witness_auto_requeue_expired,
            )
            if self.witness_hub_enable:
                self._start_witness_hub_server()
            if self.keeper_account and self.witness_enabled:
                self._witness_worker_thread = threading.Thread(target=self._witness_worker_loop, daemon=True)
                self._witness_worker_thread.start()
        else:
            logger.warning("见证签名模式未启用（WITNESS_SIGNER_ADDRESSES 必须配置为3个地址）")

    def _parse_witness_signers(self) -> list[str]:
        raw = os.getenv("WITNESS_SIGNER_ADDRESSES", "").strip()
        if not raw:
            return []
        addrs: list[str] = []
        seen: set[str] = set()

        def _push_addr(val: str) -> None:
            try:
                a = Web3.to_checksum_address(val)
            except Exception:
                return
            low = a.lower()
            if low in seen:
                return
            seen.add(low)
            addrs.append(a)

        try:
            val = json.loads(raw)
            if isinstance(val, list):
                for x in val:
                    a = str(x).strip()
                    if not a:
                        continue
                    _push_addr(a)
                return addrs
        except Exception:
            pass
        for x in raw.replace("\n", ",").split(","):
            a = x.strip()
            if not a:
                continue
            _push_addr(a)
        return addrs

    def _build_witness_struct_hash(self, user: str, amount: int, tx_hash_bytes: bytes, deadline: int) -> str:
        user_addr = Web3.to_checksum_address(user)

        encoded = abi_encode(
            ["bytes32", "uint256", "address", "address", "uint256", "bytes32", "uint256"],
            [
                bytes(WITNESS_TYPEHASH),
                int(self.w3.eth.chain_id),
                self.controller_address,
                user_addr,
                int(amount),
                tx_hash_bytes,
                int(deadline),
            ],
        )
        return Web3.keccak(encoded).hex()

    def _normalize_sig_hex(self, signature: str) -> str:
        s = str(signature or "").strip()
        if not s:
            return ""
        if not s.startswith("0x"):
            s = "0x" + s
        h = s[2:]
        if len(h) != 130:
            return ""
        try:
            raw = bytes.fromhex(h)
        except Exception:
            return ""
        if len(raw) != 65:
            return ""

        r = int.from_bytes(raw[0:32], "big")
        s_val = int.from_bytes(raw[32:64], "big")
        v = raw[64]
        if v < 27:
            v += 27
        if v not in (27, 28):
            return ""

        if s_val > SECP256K1_N_HALF:
            s_val = SECP256K1_N - s_val
            v = 27 if v == 28 else 28

        norm = (
            r.to_bytes(32, "big")
            + s_val.to_bytes(32, "big")
            + bytes([v])
        )
        return "0x" + norm.hex()

    def _witness_signer_allowed(self, signer: str) -> bool:
        try:
            signer_c = Web3.to_checksum_address(signer)
            return signer_c in self.witness_signers
        except Exception:
            return False

    def _verify_witness_signature(self, struct_hash_hex: str, signer: str, signature_hex: str) -> bool:
        try:
            msg = encode_defunct(hexstr=struct_hash_hex)
            sig_bytes = Web3.to_bytes(hexstr=signature_hex)
            recovered = Account.recover_message(msg, signature=sig_bytes)
            return Web3.to_checksum_address(recovered) == Web3.to_checksum_address(signer)
        except Exception:
            return False

    def _enqueue_witness_task(self, transfer: dict) -> bool:
        txh = str(transfer.get("txHash", "")).lower()
        if not txh:
            return False
        tx_hash_bytes = Web3.to_bytes(hexstr=txh)
        deadline = int(time.time()) + self.witness_deadline_seconds
        struct_hash = self._build_witness_struct_hash(
            transfer["from"], int(transfer["amount"]), tx_hash_bytes, deadline
        )
        task = {
            "txHash": txh,
            "user": transfer["from"],
            "amount": int(transfer["amount"]),
            "blockNumber": int(transfer.get("blockNumber") or 0),
            "witnessDeadline": int(deadline),
            "structHash": struct_hash,
            "signatures": {},
            "status": "pending",
            "createdAt": int(time.time()),
            "lastError": "",
            "submitAttempts": 0,
            "submitNonce": None,
            "lastSubmitTxHash": "",
            "lastSubmitAt": 0,
            "nextBumpBps": 10000,
        }
        with self._witness_lock:
            if txh in self._witness_tasks:
                return False
            self._witness_tasks[txh] = task
        logger.info("见证任务已入队: tx=%s user=%s amount=%s", txh, task["user"], task["amount"] / 10**18)
        return True

    def _is_transfer_processed_quiet(self, tx_hash: str) -> Optional[bool]:
        try:
            tx_hash_bytes = Web3.to_bytes(hexstr=tx_hash)
            return bool(self.controller_contract.functions.processedTransfers(tx_hash_bytes).call())
        except Exception as e:
            logger.debug("静默检查 processedTransfers 失败: tx=%s err=%s", tx_hash, e)
            return None

    def _refresh_witness_task_deadline(self, task: dict, reason: str) -> None:
        txh = str(task.get("txHash") or "").lower()
        user = str(task.get("user") or "")
        amount = int(task.get("amount") or 0)
        if not txh or not user or amount <= 0:
            task["status"] = "failed"
            task["lastError"] = "deadline_refresh_bad_task"
            return

        old_deadline = int(task.get("witnessDeadline") or 0)
        old_signed = len(task.get("signatures") or {})
        new_deadline = int(time.time()) + self.witness_deadline_seconds
        tx_hash_bytes = Web3.to_bytes(hexstr=txh)
        new_struct_hash = self._build_witness_struct_hash(user, amount, tx_hash_bytes, new_deadline)

        task["witnessDeadline"] = new_deadline
        task["structHash"] = new_struct_hash
        task["signatures"] = {}
        task["status"] = "pending"
        task["lastError"] = f"deadline_refreshed:{reason}"

        task["submitAttempts"] = 0
        task["submitNonce"] = None
        task["lastSubmitTxHash"] = ""
        task["lastSubmitAt"] = 0
        task["nextBumpBps"] = 10000
        task["createdAt"] = int(time.time())

        logger.info(
            "见证任务刷新deadline并重签: tx=%s reason=%s old_deadline=%s new_deadline=%s cleared_sigs=%s",
            txh,
            reason,
            old_deadline,
            new_deadline,
            old_signed,
        )

    def _submit_witness_signature(self, tx_hash: str, signer: str, signature: str) -> tuple[bool, str]:
        txh = str(tx_hash or "").strip().lower()
        sig_hex = self._normalize_sig_hex(signature)
        if not txh or not sig_hex:
            return False, "bad_params"
        if not self._witness_signer_allowed(signer):
            return False, "signer_not_allowed"
        with self._witness_lock:
            task = self._witness_tasks.get(txh)
            if not task:
                return False, "task_not_found"
            if task.get("status") != "pending":
                return False, f"task_status_{task.get('status')}"
            if int(time.time()) > int(task.get("witnessDeadline") or 0):
                task["status"] = "expired"
                return False, "expired"
            signer_c = Web3.to_checksum_address(signer)
            if signer_c in task["signatures"]:
                return True, "already_signed"
            struct_hash = str(task["structHash"])
            if not self._verify_witness_signature(struct_hash, signer_c, sig_hex):
                return False, "bad_signature"
            task["signatures"][signer_c] = sig_hex
            signed_count = len(task["signatures"])
        return True, f"signed_{signed_count}"

    def _list_pending_witness_tasks(self, signer: Optional[str] = None) -> list[dict]:
        now = int(time.time())
        out: list[dict] = []
        signer_c = None
        if signer:
            try:
                signer_c = Web3.to_checksum_address(signer)
            except Exception:
                signer_c = None
        with self._witness_lock:
            for txh, task in self._witness_tasks.items():
                if task.get("status") != "pending":
                    continue
                if now > int(task.get("witnessDeadline") or 0):
                    task["status"] = "expired"
                    continue
                if signer_c and signer_c in task["signatures"]:
                    continue
                out.append(
                    {
                        "txHash": txh,
                        "user": task["user"],
                        "amount": str(task["amount"]),
                        "blockNumber": task["blockNumber"],
                        "witnessDeadline": task["witnessDeadline"],
                        "structHash": task["structHash"],
                        "signedBy": list(task["signatures"].keys()),
                        "required": self.witness_threshold,
                    }
                )
        return out

    def _finalize_witness_success(self, task: dict, receipt) -> bool:
        is_refund = self._receipt_is_refund(receipt)
        if is_refund:
            return True

        tx_hash = task["txHash"]
        user = task["user"]
        amount = int(task["amount"])
        block_number = task.get("blockNumber")
        ts = self._get_block_ts(block_number)
        if ts is None:
            ts = int(time.time())
        self._record_deposit_point(ts, int(amount))
        if block_number is not None:
            self._record_deposit_detail(
                user_address=user,
                amount_wei=int(amount),
                tx_hash=tx_hash,
                block_number=int(block_number),
                block_timestamp=int(ts),
            )

        self._parse_and_store_contract_events(receipt, original_tx_hash=tx_hash)
        return False

    def _tx_receipt_if_exists(self, tx_hash: str):
        if not tx_hash:
            return None
        try:
            return self.w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return None

    def _tx_still_known(self, tx_hash: str) -> bool:
        if not tx_hash:
            return False
        try:
            _ = self.w3.eth.get_transaction(tx_hash)
            return True
        except Exception:
            return False

    def _check_submitted_witness_tx(self, task: dict) -> tuple[str, bool]:
        last_tx = str(task.get("lastSubmitTxHash") or "")
        if not last_tx:
            return "missing", False

        receipt = self._tx_receipt_if_exists(last_tx)
        if receipt is not None:
            status = getattr(receipt, "status", None)
            if status is None and isinstance(receipt, dict):
                status = receipt.get("status")
            if int(status or 0) == 1:
                is_refund = self._finalize_witness_success(task, receipt)
                return "confirmed", is_refund
            return "reverted", False

        if self._tx_still_known(last_tx):
            return "pending", False
        return "missing", False

    def _call_deposit_from_transfer_witness(self, task: dict) -> tuple[str, bool]:
        if not self.keeper_account:
            logger.error("未配置keeper私钥，无法提交见证记账交易")
            return "failed", False
        signatures_map: dict = task.get("signatures") or {}
        sig_bytes = []
        for signer in self.witness_signers:
            sig = signatures_map.get(signer)
            if not sig:
                continue
            sig_bytes.append(Web3.to_bytes(hexstr=sig))
            if len(sig_bytes) >= self.witness_threshold:
                break
        if len(sig_bytes) < self.witness_threshold:
            return "retry", False

        tx_hash = task["txHash"]
        user = task["user"]
        amount = int(task["amount"])
        deadline = int(task["witnessDeadline"])
        if int(time.time()) > deadline:
            task["lastError"] = "witness_expired_precheck"
            return "expired", False

        tx_hash_sent = ""
        try:
            user_address = Web3.to_checksum_address(user)
            tx_hash_bytes = Web3.to_bytes(hexstr=tx_hash)
            function = self.controller_contract.functions.depositFromTransferWitness(
                user_address,
                amount,
                tx_hash_bytes,
                deadline,
                sig_bytes
            )
            try:
                gas_estimate = function.estimate_gas({'from': self.keeper_address})
                gas_limit = int(gas_estimate * 1.2)
            except Exception as e:
                msg = str(e).lower()
                if "witness_expired" in msg:
                    logger.warning("见证任务已过期（估算阶段）: tx=%s deadline=%s", tx_hash, deadline)
                    task["lastError"] = "witness_expired_estimate"
                    return "expired", False
                if "insufficient_witness_sigs" in msg or "sigs_lt_threshold" in msg:

                    logger.info("见证签名尚未达链上有效门槛，稍后重试: tx=%s", tx_hash)
                    return "retry", False
                logger.warning(f"Witness模式Gas估算失败，使用默认值: {e}")
                gas_limit = 260000

            nonce = task.get("submitNonce")
            if nonce is None:
                nonce = self._allocate_nonce()
            else:
                nonce = int(nonce)
            bump_bps = max(10000, int(task.get("nextBumpBps") or 10000))
            bump_bps = min(bump_bps, self.tx_max_bump_bps)
            gas_price = self._current_gas_price(bump_bps)

            transaction = function.build_transaction({
                'from': self.keeper_address,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
                'chainId': self.w3.eth.chain_id,
            })
            signed_txn = self.keeper_account.sign_transaction(transaction)
            raw = getattr(signed_txn, "rawTransaction", None) or getattr(signed_txn, "raw_transaction", None)
            if raw is None:
                raise RuntimeError("SignedTransaction missing raw tx bytes")

            tx_hash_sent_bytes = self.w3.eth.send_raw_transaction(raw)
            tx_hash_sent = tx_hash_sent_bytes.hex()
            task["submitNonce"] = nonce
            task["lastSubmitTxHash"] = tx_hash_sent
            task["lastSubmitAt"] = int(time.time())
            task["submitAttempts"] = int(task.get("submitAttempts") or 0) + 1
            next_bump = max(
                self.retry_gas_price_bump_bps,
                int((bump_bps * self.retry_gas_price_bump_bps) // 10000),
            )
            task["nextBumpBps"] = min(next_bump, self.tx_max_bump_bps)
            logger.info(
                "已发送见证记账交易: %s orig=%s nonce=%s gasPrice=%s bump=%sbps attempts=%s",
                tx_hash_sent,
                tx_hash,
                nonce,
                gas_price,
                bump_bps,
                task["submitAttempts"],
            )

            if not self.wait_for_receipt:
                return "submitted", False

            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash_sent_bytes, timeout=self.receipt_timeout)
            except Exception as e:
                msg = str(e).lower()
                if "not in the chain" in msg or "timeexhausted" in msg or "timeout" in msg:

                    logger.warning(
                        "见证记账等待回执超时，保留为pending后续确认/替换: tx=%s orig=%s err=%s",
                        tx_hash_sent,
                        tx_hash,
                        e,
                    )
                    return "submitted", False
                raise

            status = getattr(receipt, "status", None)
            if status is None and isinstance(receipt, dict):
                status = receipt.get("status")
            if int(status or 0) == 1:
                is_refund = self._finalize_witness_success(task, receipt)
                return "confirmed", is_refund

            logger.error(f"见证记账交易失败(status=0): {tx_hash_sent}")
            return "failed", False
        except Exception as e:
            msg = str(e).lower()
            if "witness_expired" in msg:
                logger.warning("见证任务已过期（提交阶段）: tx=%s err=%s", tx_hash, e)
                task["lastError"] = "witness_expired_submit"
                return "expired", False
            if (
                "nonce too low" in msg
                or "replacement transaction underpriced" in msg
                or "already known" in msg
                or "nonce has already been used" in msg
            ):
                logger.warning(f"见证记账nonce冲突，刷新后重试: {e}")
                self._refresh_nonce()
                task["submitNonce"] = None
                return "retry", False
            logger.error(f"提交见证记账失败: {e}")
            task["lastError"] = str(e)
            return "retry", False

    def _flush_ready_witness_tasks(self) -> None:
        now = int(time.time())
        ready: list[str] = []
        with self._witness_lock:
            for txh, task in list(self._witness_tasks.items()):
                status = str(task.get("status") or "")
                if status == "done":
                    continue

                if status in ("expired", "failed"):
                    should_requeue = self.witness_auto_requeue_expired and (
                        status == "expired"
                        or "witness_expired" in str(task.get("lastError") or "").lower()
                    )
                    if not should_requeue:
                        continue
                    processed = self._is_transfer_processed_quiet(txh)
                    if processed is True:
                        task["status"] = "done"
                        task["lastError"] = ""
                        continue
                    self._refresh_witness_task_deadline(task, f"terminal_{status}")
                    status = "pending"

                deadline = int(task.get("witnessDeadline") or 0)
                signed_count = len(task.get("signatures") or {})

                if status == "pending":
                    remaining = deadline - now
                    if remaining <= 0:
                        self._refresh_witness_task_deadline(task, "pending_deadline_passed")
                        continue
                    if (
                        signed_count < self.witness_threshold
                        and remaining <= self.witness_deadline_refresh_margin_seconds
                    ):
                        self._refresh_witness_task_deadline(task, "pending_near_expiry")
                        continue

                if status == "pending" and signed_count >= self.witness_threshold:
                    ready.append(txh)
                elif status == "submitting" and str(task.get("lastSubmitTxHash") or ""):
                    ready.append(txh)

        for txh in ready:
            with self._witness_lock:
                cur = self._witness_tasks.get(txh)
                if not cur:
                    continue
                status = str(cur.get("status") or "pending")
                if status in ("done", "expired", "failed"):
                    continue
                task = dict(cur)

            if status == "submitting":
                tx_state, _ = self._check_submitted_witness_tx(task)
                if tx_state == "confirmed":
                    with self._witness_lock:
                        live = self._witness_tasks.get(txh)
                        if live:
                            live["status"] = "done"
                            live["lastError"] = ""
                    continue
                if tx_state == "reverted":
                    should_requeue = (
                        self.witness_auto_requeue_expired
                        and int(time.time()) > int(task.get("witnessDeadline") or 0)
                    )
                    processed = self._is_transfer_processed_quiet(txh) if should_requeue else None
                    with self._witness_lock:
                        live = self._witness_tasks.get(txh)
                        if live:
                            if should_requeue and processed is not True:
                                self._refresh_witness_task_deadline(live, "submitting_reverted_after_deadline")
                            else:
                                live["status"] = "failed"
                                live["lastError"] = "onchain_reverted"
                    continue
                if tx_state == "pending":
                    last_submit_at = int(task.get("lastSubmitAt") or 0)
                    if last_submit_at > 0 and now - last_submit_at < self.tx_replace_interval_seconds:
                        continue

            submit_attempts = int(task.get("submitAttempts") or 0)
            if submit_attempts >= self.tx_max_replace_attempts:
                with self._witness_lock:
                    live = self._witness_tasks.get(txh)
                    if live:
                        live["status"] = "failed"
                        live["lastError"] = "max_replace_attempts_reached"
                continue

            task["status"] = "submitting"
            result, _ = self._call_deposit_from_transfer_witness(task)

            with self._witness_lock:
                live = self._witness_tasks.get(txh)
                if not live:
                    continue

                for k in (
                    "submitAttempts",
                    "submitNonce",
                    "lastSubmitTxHash",
                    "lastSubmitAt",
                    "nextBumpBps",
                    "lastError",
                ):
                    if k in task:
                        live[k] = task[k]
                if result == "confirmed":
                    live["status"] = "done"
                    live["lastError"] = ""
                elif result == "submitted":

                    live["status"] = "submitting"
                elif result == "expired":
                    processed = self._is_transfer_processed_quiet(txh)
                    if processed is True:
                        live["status"] = "done"
                        live["lastError"] = ""
                    elif self.witness_auto_requeue_expired:
                        self._refresh_witness_task_deadline(live, "submit_result_expired")
                    else:
                        live["status"] = "expired"
                        if not live.get("lastError"):
                            live["lastError"] = "witness_expired"
                elif result == "retry":
                    if int(time.time()) > int(live.get("witnessDeadline") or 0):
                        if self.witness_auto_requeue_expired:
                            processed = self._is_transfer_processed_quiet(txh)
                            if processed is True:
                                live["status"] = "done"
                                live["lastError"] = ""
                            else:
                                self._refresh_witness_task_deadline(live, "retry_after_deadline")
                        else:
                            live["status"] = "expired"
                    else:

                        if str(live.get("lastSubmitTxHash") or ""):
                            live["status"] = "submitting"
                        else:
                            live["status"] = "pending"
                else:
                    if int(time.time()) > int(live.get("witnessDeadline") or 0):
                        if self.witness_auto_requeue_expired:
                            processed = self._is_transfer_processed_quiet(txh)
                            if processed is True:
                                live["status"] = "done"
                                live["lastError"] = ""
                            else:
                                self._refresh_witness_task_deadline(live, "failed_after_deadline")
                        else:
                            live["status"] = "expired"
                    else:
                        live["status"] = "failed"
                        if not live.get("lastError"):
                            live["lastError"] = "submit_failed"
        self._prune_witness_tasks()

    def _prune_witness_tasks(self) -> None:
        now = int(time.time())
        retention = self.witness_task_retention_seconds
        with self._witness_lock:
            for txh, task in list(self._witness_tasks.items()):
                status = str(task.get("status") or "")
                if status not in ("done", "expired", "failed"):
                    continue
                created_at = int(task.get("createdAt") or now)
                if now - created_at >= retention:
                    self._witness_tasks.pop(txh, None)

    def _witness_worker_loop(self) -> None:
        while not self._witness_worker_stop:
            try:
                self._flush_ready_witness_tasks()
            except Exception as e:
                logger.warning(f"witness worker loop error: {e}")
            time.sleep(self.witness_worker_interval)

    def _check_hub_auth(self, headers) -> bool:
        if not self.witness_hub_api_key:
            return True
        got = str(headers.get("X-Api-Key") or "").strip()
        return got == self.witness_hub_api_key

    def _normalize_tx_hash(self, tx_hash: str) -> str:
        txh = str(tx_hash or "").strip()
        if not txh:
            return ""
        if not txh.startswith("0x"):
            txh = "0x" + txh
        try:
            raw = Web3.to_bytes(hexstr=txh)
        except Exception:
            return ""
        if len(raw) != 32:
            return ""
        return "0x" + raw.hex()

    def _extract_transfer_from_tx_hash(self, tx_hash: str) -> tuple[Optional[dict], str]:
        receipt = self._tx_receipt_if_exists(tx_hash)
        if not receipt:
            return None, "tx_not_found"

        candidates: list[dict] = []
        for lg in receipt.get("logs") or []:
            try:
                addr = str(lg.get("address") or "")
                if not addr or addr.lower() != self.usdt_address.lower():
                    continue
                topics = lg.get("topics") or []
                if len(topics) < 3:
                    continue
                topic0 = self._to_hex_str(topics[0])
                if isinstance(topic0, str) and not topic0.startswith("0x"):
                    topic0 = "0x" + topic0
                if str(topic0).lower() != self.TRANSFER_EVENT_TOPIC.lower():
                    continue

                transfer = self.parse_transfer_event(lg)
                if not transfer:
                    continue
                if str(transfer.get("to") or "").lower() != self.controller_address.lower():
                    continue
                if self.pool_seeder_address and str(transfer.get("from") or "").lower() == self.pool_seeder_address.lower():
                    continue

                transfer["txHash"] = tx_hash
                if transfer.get("blockNumber") is None:
                    transfer["blockNumber"] = receipt.get("blockNumber")
                candidates.append(transfer)
            except Exception:
                continue

        if not candidates:
            return None, "transfer_not_found"
        if len(candidates) > 1:
            return None, "multiple_transfer_logs"
        return candidates[0], "ok"

    def enqueue_transfer_by_hash(self, tx_hash: str) -> tuple[bool, str, Optional[dict]]:
        try:
            txh = self._normalize_tx_hash(tx_hash)
            if not txh:
                return False, "bad_tx_hash", None

            processed = self._is_transfer_processed_quiet(txh)
            if processed is True:
                return True, "already_processed", None

            with self._witness_lock:
                if txh in self._witness_tasks:
                    return True, "already_queued", self._witness_tasks.get(txh)

            transfer, detail = self._extract_transfer_from_tx_hash(txh)
            if not transfer:
                return False, detail, None

            ok = self.process_transfer(transfer)
            if ok:
                return True, "queued", transfer

            processed = self._is_transfer_processed_quiet(txh)
            if processed is True:
                return True, "already_processed", transfer

            with self._witness_lock:
                if txh in self._witness_tasks:
                    return True, "already_queued", transfer

            if not self.witness_enabled:
                return False, "witness_disabled", transfer
            return False, "enqueue_failed", transfer
        except Exception as e:
            logger.exception("enqueue_transfer_by_hash crashed: tx=%s err=%s", tx_hash, e)
            return False, "enqueue_exception", None

    def _start_witness_hub_server(self) -> None:
        listener = self

        class WitnessHubHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug("witness-hub: " + format, *args)

            def _write_json(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._write_json(200, {"ok": True})
                    return
                if parsed.path == "/v1/info":
                    if not listener._check_hub_auth(self.headers):
                        self._write_json(401, {"ok": False, "error": "unauthorized"})
                        return
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "chainId": int(listener.w3.eth.chain_id),
                            "controller": listener.controller_address,
                            "witnessSigners": listener.witness_signers,
                            "witnessThreshold": listener.witness_threshold,
                            "witnessTypehash": WITNESS_TYPEHASH.hex(),
                        },
                    )
                    return
                if parsed.path == "/v1/pending":
                    if not listener._check_hub_auth(self.headers):
                        self._write_json(401, {"ok": False, "error": "unauthorized"})
                        return
                    qs = parse_qs(parsed.query or "")
                    signer = (qs.get("signer") or [""])[0].strip()
                    items = listener._list_pending_witness_tasks(signer=signer or None)
                    self._write_json(200, {"ok": True, "items": items})
                    return
                self._write_json(404, {"ok": False, "error": "not_found"})

            def do_POST(self):
                try:
                    parsed = urlparse(self.path)
                    if parsed.path not in ("/v1/sign", "/v1/enqueue_hash"):
                        self._write_json(404, {"ok": False, "error": "not_found"})
                        return
                    if not listener._check_hub_auth(self.headers):
                        self._write_json(401, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        n = int(self.headers.get("Content-Length") or "0")
                        raw = self.rfile.read(max(0, n))
                        data = json.loads(raw.decode("utf-8") if raw else "{}")
                    except Exception:
                        self._write_json(400, {"ok": False, "error": "bad_json"})
                        return

                    if parsed.path == "/v1/sign":
                        ok, detail = listener._submit_witness_signature(
                            str(data.get("txHash") or ""),
                            str(data.get("signer") or ""),
                            str(data.get("signature") or ""),
                        )
                        self._write_json(200 if ok else 400, {"ok": ok, "detail": detail})
                        return

                    ok, detail, transfer = listener.enqueue_transfer_by_hash(str(data.get("txHash") or ""))
                    payload = {"ok": ok, "detail": detail}
                    if transfer:
                        amount_raw = transfer.get("amount")
                        block_raw = transfer.get("blockNumber")
                        try:
                            amount_s = str(int(amount_raw or 0))
                        except Exception:
                            amount_s = "0"
                        try:
                            block_i = int(block_raw or 0)
                        except Exception:
                            block_i = 0
                        payload["transfer"] = {
                            "txHash": str(transfer.get("txHash") or ""),
                            "from": str(transfer.get("from") or transfer.get("user") or ""),
                            "to": str(transfer.get("to") or listener.controller_address),
                            "amount": amount_s,
                            "blockNumber": block_i,
                        }
                    self._write_json(200 if ok else 400, payload)
                except Exception as e:
                    logger.exception("witness-hub do_POST crashed: path=%s err=%s", self.path, e)
                    self._write_json(500, {"ok": False, "error": "hub_internal_error"})

        try:
            server = ThreadingHTTPServer((self.witness_hub_host, self.witness_hub_port), WitnessHubHandler)
        except Exception as e:
            logger.warning(f"启动见证Hub失败，将禁用见证提交流程: {e}")
            self.witness_enabled = False
            return

        self._witness_hub_server = server
        self._witness_hub_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._witness_hub_thread.start()
        logger.info("见证Hub已启动: http://%s:%s", self.witness_hub_host, self.witness_hub_port)

    def _get_ws_conn(self):
        if not self.w3_sub:
            return None
        provider = getattr(self.w3_sub, "provider", None)
        if provider is None:
            return None
        for attr in ("ws", "_ws", "websocket", "_websocket"):
            ws = getattr(provider, attr, None)
            if ws is not None and hasattr(ws, "recv"):
                return ws
        return None

    def _init_stats_db(self) -> None:
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")

            conn.execute(
                "CREATE TABLE IF NOT EXISTS deposit_points ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts INTEGER NOT NULL, "
                "amount_wei TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_points_ts ON deposit_points(ts)")

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

    def _purge_old_points(self, conn: sqlite3.Connection, now_ts: int) -> None:
        if self.price_retention_days <= 0:
            return
        cutoff = now_ts - (self.price_retention_days * 86400)
        conn.execute("DELETE FROM deposit_points WHERE ts < ?", (cutoff,))

    def _get_block_ts(self, block_number: Optional[int]) -> Optional[int]:
        if block_number is None:
            return None
        if block_number in self._block_ts_cache:
            return self._block_ts_cache[block_number]
        try:
            ts = int(self.w3.eth.get_block(block_number)["timestamp"])
            self._block_ts_cache[block_number] = ts

            if len(self._block_ts_cache) > 5000:
                self._block_ts_cache.pop(next(iter(self._block_ts_cache)))
            return ts
        except Exception as e:
            logger.debug(f"get_block_ts failed: {e}")
            return None

    def _record_deposit_point(self, ts: int, amount_wei: int) -> None:
        if ts <= 0 or amount_wei <= 0:
            return
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        with conn:
            conn.execute(
                "INSERT INTO deposit_points(ts, amount_wei) VALUES(?, ?)",
                (ts, str(amount_wei)),
            )
            if ts % 3600 == 0:
                self._purge_old_points(conn, ts)
        conn.close()

    def _record_deposit_detail(self, user_address: str, amount_wei: int, tx_hash: str,
                               block_number: int, block_timestamp: int,
                               referrer_address: Optional[str] = None,
                               power_added: Optional[int] = None) -> None:
        try:
            conn = sqlite3.connect(self.price_db_path, timeout=30)
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO deposit_records "
                    "(user_address, amount_wei, tx_hash, block_number, block_timestamp, "
                    "referrer_address, power_added, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_address.lower(), str(amount_wei), tx_hash.lower(), block_number,
                     block_timestamp, referrer_address.lower() if referrer_address else None,
                     str(power_added) if power_added is not None else None, int(time.time()))
                )
            conn.close()
        except Exception as e:
            logger.error(f"存储入金明细失败: {e}")

    def _record_referral_relation(self, user_address: str, referrer_address: str,
                                   block_number: int, block_timestamp: int, tx_hash: str) -> None:
        try:
            conn = sqlite3.connect(self.price_db_path, timeout=30)
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO referral_relations "
                    "(user_address, referrer_address, block_number, block_timestamp, tx_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_address.lower(), referrer_address.lower(), block_number,
                     block_timestamp, tx_hash.lower(), int(time.time()))
                )
            conn.close()
        except Exception as e:
            logger.error(f"存储推荐关系失败: {e}")

    def _receipt_is_refund(self, receipt) -> bool:
        try:
            for lg in receipt.get("logs", []) or []:
                addr = lg.get("address", "")
                if not addr:
                    continue
                if addr.lower() != self.controller_address.lower():
                    continue
                topics = lg.get("topics") or []
                if not topics:
                    continue
                t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                if isinstance(t0, str) and not t0.startswith("0x"):
                    t0 = "0x" + t0
                if t0.lower() == self.REFUND_EVENT_TOPIC.lower():
                    return True
            return False
        except Exception:
            return False

    def _to_hex_str(self, x) -> str:
        if isinstance(x, str):
            return x
        if isinstance(x, (bytes, bytearray)):
            return '0x' + bytes(x).hex()
        if hasattr(x, "hex"):
            h = x.hex()
            if isinstance(h, str):
                return h if h.startswith('0x') else '0x' + h
        return str(x)

    def _parse_and_store_contract_events(self, receipt, original_tx_hash: Optional[str] = None) -> None:
        try:
            block_number = receipt.get("blockNumber")
            block_ts = self._get_block_ts(block_number)
            if block_ts is None:
                block_ts = int(time.time())

            bookkeeping_tx_hash = self._to_hex_str(receipt.get("transactionHash"))
            target_tx_hash = (original_tx_hash or bookkeeping_tx_hash or "").lower()

            for lg in receipt.get("logs", []) or []:
                addr = lg.get("address", "")
                if not addr or addr.lower() != self.controller_address.lower():
                    continue

                topics = lg.get("topics") or []
                if not topics:
                    continue

                t0 = self._to_hex_str(topics[0])
                if not t0.startswith("0x"):
                    t0 = "0x" + t0

                if t0.lower() == self.DEPOSIT_DETAIL_TOPIC.lower() and len(topics) >= 3:
                    try:
                        user = Web3.to_checksum_address('0x' + self._to_hex_str(topics[1])[-40:])
                        referrer = Web3.to_checksum_address('0x' + self._to_hex_str(topics[2])[-40:])
                        data_hex = self._to_hex_str(lg.get("data"))

                        if len(data_hex) >= 130:
                            usdt_amount = int(data_hex[2:66], 16)
                            power_added = int(data_hex[66:130], 16)

                            conn = sqlite3.connect(self.price_db_path, timeout=30)
                            with conn:
                                conn.execute(
                                    "UPDATE deposit_records SET referrer_address = ?, power_added = ? "
                                    "WHERE tx_hash = ?",
                                    (referrer.lower() if referrer != "0x0000000000000000000000000000000000000000" else None,
                                     str(power_added), target_tx_hash)
                                )
                            conn.close()

                            if referrer != "0x0000000000000000000000000000000000000000":
                                self._record_referral_relation(
                                    user_address=user,
                                    referrer_address=referrer,
                                    block_number=block_number,
                                    block_timestamp=block_ts,
                                    tx_hash=target_tx_hash or bookkeeping_tx_hash
                                )

                            logger.debug(
                                f"更新入金明细: tx={target_tx_hash or bookkeeping_tx_hash}, user={user}, referrer={referrer}, power={power_added}"
                            )
                    except Exception as e:
                        logger.debug(f"解析 Deposit 事件失败: {e}")

                elif t0.lower() == self.REFERRAL_BOUND_TOPIC.lower() and len(topics) >= 3:
                    try:
                        user = Web3.to_checksum_address('0x' + self._to_hex_str(topics[1])[-40:])
                        inviter = Web3.to_checksum_address('0x' + self._to_hex_str(topics[2])[-40:])

                        self._record_referral_relation(
                            user_address=user,
                            referrer_address=inviter,
                            block_number=block_number,
                            block_timestamp=block_ts,
                            tx_hash=target_tx_hash or bookkeeping_tx_hash
                        )

                        logger.debug(f"存储推荐关系: user={user}, inviter={inviter}")
                    except Exception as e:
                        logger.debug(f"解析 ReferralBound 事件失败: {e}")
        except Exception as e:
            logger.error(f"解析合约事件失败: {e}")

    def _subscribe(self, sub_type: str, sub_filter: Optional[dict] = None) -> Optional[str]:
        try:
            if not self.use_subscription:
                return None
            if not self.w3_sub:
                return None
            if self._get_ws_conn() is None:
                return None

            params = [sub_type]
            if sub_filter is not None:
                params.append(sub_filter)

            resp = self.w3_sub.provider.make_request("eth_subscribe", params)
            sub_id = resp.get("result") if isinstance(resp, dict) else None
            if not sub_id:
                return None
            return sub_id
        except Exception:
            return None

    def _subscribe_logs(self) -> Optional[str]:
        try:
            if not self.use_websocket or not self.w3_sub:
                return None
            if self._get_ws_conn() is None:
                if not self._disabled_subscription_due_to_no_recv:
                    logger.warning("当前 WebSocketProvider 无法读取推送消息（缺少 recv），将关闭订阅并回退到轮询（可设置 USE_SUBSCRIPTION=false 静默）")
                    self._disabled_subscription_due_to_no_recv = True

                self.use_subscription = False
                return None

            sub_id = self._subscribe(
                "logs",
                {
                    "address": self.usdt_address,
                    "topics": [
                        self.TRANSFER_EVENT_TOPIC,
                        None,
                        self._target_to_topic(),
                    ],
                },
            )
            if not sub_id:
                logger.warning("节点不支持/拒绝 eth_subscribe(logs)，回退到轮询")
                return None
            logger.info(f"已启用订阅模式（logs），subscription id: {sub_id}")
            return sub_id
        except Exception as e:
            logger.warning(f"订阅初始化失败，回退到轮询: {e}")
            return None

    def _subscribe_new_heads(self) -> Optional[str]:
        try:
            if not self.use_websocket or not self.w3_sub:
                return None
            sub_id = self._subscribe("newHeads")
            if not sub_id:
                return None
            logger.info(f"已订阅 newHeads，subscription id: {sub_id}")
            return sub_id
        except Exception:
            return None

    def _run_subscription_loop(self) -> bool:
        logs_sub_id = self._subscribe_logs()
        if not logs_sub_id:
            return False

        heads_sub_id = self._subscribe_new_heads()
        if self.confirmations > 0 and not heads_sub_id:
            logger.warning("订阅模式下 confirmations>0 但无法订阅 newHeads，回退到轮询")
            return False

        ws = self._get_ws_conn()
        if ws is None:
            return False

        logger.info("开始订阅消费USDT入金事件（推送模式，含确认数处理）...")

        latest_head: Optional[int] = None
        pending_by_block: dict[int, list[dict]] = {}

        while True:
            try:
                raw = ws.recv()
                if not raw:
                    continue

                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")

                msg = json.loads(raw)

                if not isinstance(msg, dict) or msg.get("method") != "eth_subscription":
                    continue

                params = msg.get("params") or {}
                sub = params.get("subscription")
                result = params.get("result")

                if heads_sub_id and sub == heads_sub_id and isinstance(result, dict):
                    try:
                        num_hex = result.get("number")
                        if isinstance(num_hex, str):
                            latest_head = int(num_hex, 16)
                    except Exception:
                        pass

                if sub == logs_sub_id and isinstance(result, dict):
                    try:

                        if result.get("removed") is True:

                            bn = result.get("blockNumber")
                            if isinstance(bn, str):
                                bni = int(bn, 16)
                                pending_by_block.pop(bni, None)
                            continue

                        bn = result.get("blockNumber")
                        if isinstance(bn, str):
                            bni = int(bn, 16)
                        elif isinstance(bn, int):
                            bni = bn
                        else:
                            continue

                        pending_by_block.setdefault(bni, []).append(result)
                    except Exception:
                        continue

                confirmed_head = None
                if self.confirmations == 0:
                    confirmed_head = float("inf")
                elif latest_head is not None:
                    confirmed_head = latest_head - self.confirmations

                if confirmed_head is None:
                    continue

                ready_blocks = [b for b in pending_by_block.keys() if b <= confirmed_head]
                if not ready_blocks:
                    continue

                for b in sorted(ready_blocks):
                    logs = pending_by_block.pop(b, [])
                    for log_obj in logs:
                        transfer = self.parse_transfer_event(log_obj)
                        if not transfer:
                            continue

                        if transfer.get("blockNumber") is not None:
                            try:
                                self.current_block = max(self.current_block, int(transfer["blockNumber"]) + 1)
                            except Exception:
                                pass

                        if self.keeper_account:
                            self._inflight_sem.acquire()
                            self._tx_executor.submit(self._process_transfer_task, transfer)
                        else:
                            self.process_transfer(transfer)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.warning(f"订阅消费异常，将回退到轮询: {e}")
                time.sleep(self.subscription_reconnect_delay)
                return False

    def _address_topic(self, addr: str) -> str:
        return '0x000000000000000000000000' + addr.lower().replace('0x', '')

    def _target_to_topic(self):

        return self._address_topic(self.controller_address)

    def _catch_up_to_safe_head(self):
        try:
            latest_block = self.w3.eth.block_number
            safe_head = latest_block - self.confirmations
            if safe_head < 0:
                safe_head = 0
            if safe_head < self.current_block:
                return

            while self.current_block <= safe_head:
                scan_end = min(safe_head, self.current_block + self.max_blocks_per_scan)
                logger.debug(f"[catchup] 扫描区块: {self.current_block} -> {scan_end}")
                events = self.get_usdt_transfer_events_parallel(self.current_block, scan_end)
                transfers = []
                for event in events:
                    t = self.parse_transfer_event(event)
                    if t:
                        transfers.append(t)
                if transfers:
                    logger.info(f"[catchup] 本段区块共发现 {len(transfers)} 笔候选入金转账")
                if self.keeper_account and transfers:
                    for t in transfers:
                        self._inflight_sem.acquire()
                        self._tx_executor.submit(self._process_transfer_task, t)
                else:
                    for t in transfers:
                        self.process_transfer(t)
                self.current_block = scan_end + 1
        except Exception as e:
            logger.warning(f"[catchup] 追块失败（将继续运行）: {e}")

    async def _ws_subscribe(self, ws, method: str, params: list, req_id: int) -> str:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        raw = await ws.recv()
        resp = json.loads(raw)
        if "error" in resp and resp["error"]:
            raise RuntimeError(resp["error"])
        sub_id = resp.get("result")
        if not sub_id:
            raise RuntimeError(f"subscribe failed: {resp}")
        return sub_id

    async def _ws_subscription_main(self):
        if not (self.use_websocket and self.ws_url and self.use_subscription):
            return False

        self._catch_up_to_safe_head()

        logger.info(f"[ws] 连接并订阅: {self.ws_url}")
        async with websockets.connect(
            self.ws_url,
            ping_interval=self.ws_ping_interval,
            ping_timeout=self.ws_ping_timeout,
            close_timeout=10,
            max_queue=1024,
        ) as ws:

            logs_filter = {
                "address": self.usdt_address,
                "topics": [self.TRANSFER_EVENT_TOPIC, None, self._target_to_topic()],
            }
            logs_sub_id = await self._ws_subscribe(ws, "eth_subscribe", ["logs", logs_filter], 1)
            heads_sub_id = None
            if self.confirmations > 0:
                heads_sub_id = await self._ws_subscribe(ws, "eth_subscribe", ["newHeads"], 2)
            logger.info(f"[ws] 订阅成功 logs={logs_sub_id} newHeads={heads_sub_id or 'disabled'} confirmations={self.confirmations}")

            latest_head: Optional[int] = None
            pending_by_block: dict[int, list[dict]] = {}

            def _maybe_process_confirmed():
                if self.confirmations <= 0:
                    return
                if latest_head is None:
                    return
                confirmed_head = latest_head - self.confirmations
                if confirmed_head < 0:
                    return
                ready_blocks = [b for b in pending_by_block.keys() if b <= confirmed_head]
                if not ready_blocks:
                    return
                for b in sorted(ready_blocks):
                    logs = pending_by_block.pop(b, [])
                    for log_obj in logs:
                        transfer = self.parse_transfer_event(log_obj)
                        if not transfer:
                            continue

                        if transfer.get("blockNumber") is not None:
                            try:
                                self.current_block = max(self.current_block, int(transfer["blockNumber"]) + 1)
                            except Exception:
                                pass
                        if self.keeper_account:
                            self._inflight_sem.acquire()
                            self._tx_executor.submit(self._process_transfer_task, transfer)
                        else:
                            self.process_transfer(transfer)

            while True:
                raw = await ws.recv()
                msg = json.loads(raw)

                if msg.get("method") != "eth_subscription":
                    continue
                params = msg.get("params") or {}
                sub = params.get("subscription")
                result = params.get("result")
                if not sub or result is None:
                    continue

                if heads_sub_id and sub == heads_sub_id:
                    try:
                        num_hex = result.get("number")
                        if isinstance(num_hex, str):
                            latest_head = int(num_hex, 16)
                            _maybe_process_confirmed()
                    except Exception:
                        pass
                    continue

                if sub == logs_sub_id:

                    try:
                        bn_hex = result.get("blockNumber")
                        bn = int(bn_hex, 16) if isinstance(bn_hex, str) else None
                    except Exception:
                        bn = None

                    if self.confirmations <= 0:
                        transfer = self.parse_transfer_event(result)
                        if transfer:
                            if transfer.get("blockNumber") is not None:
                                try:
                                    self.current_block = max(self.current_block, int(transfer["blockNumber"]) + 1)
                                except Exception:
                                    pass
                            if self.keeper_account:
                                self._inflight_sem.acquire()
                                self._tx_executor.submit(self._process_transfer_task, transfer)
                            else:
                                self.process_transfer(transfer)
                    else:
                        if bn is not None:
                            pending_by_block.setdefault(bn, []).append(result)

                            if latest_head is None:
                                try:
                                    latest_head = self.w3.eth.block_number
                                except Exception:
                                    pass
                            _maybe_process_confirmed()

                    continue

    def _run_ws_subscription_loop(self) -> bool:
        if not (self.use_websocket and self.ws_url and self.use_subscription):
            return False
        while True:
            try:
                asyncio.run(self._ws_subscription_main())
            except RuntimeError as e:

                msg = str(e).lower()
                if "-32601" in msg or "method not found" in msg or "unsupported" in msg:
                    logger.warning(f"[ws] 节点不支持 eth_subscribe，回退到轮询: {e}")
                    return False
                logger.warning(f"[ws] 订阅运行异常，将在 {self.subscription_reconnect_delay}s 后重连: {e}")
                time.sleep(self.subscription_reconnect_delay)
            except Exception as e:
                logger.warning(f"[ws] 订阅连接/消费异常，将在 {self.subscription_reconnect_delay}s 后重连: {e}")
                time.sleep(self.subscription_reconnect_delay)

    def _ensure_nonce_initialized(self):
        if not self.keeper_account:
            return
        with self._nonce_lock:
            if self._next_nonce is None:
                self._next_nonce = self.w3.eth.get_transaction_count(self.keeper_address, "pending")

    def _allocate_nonce(self) -> int:
        self._ensure_nonce_initialized()
        with self._nonce_lock:

            if self._next_nonce is None:
                self._next_nonce = self.w3.eth.get_transaction_count(self.keeper_address, "pending")
            nonce = self._next_nonce
            self._next_nonce += 1
            return nonce

    def _refresh_nonce(self):
        if not self.keeper_account:
            return
        with self._nonce_lock:
            self._next_nonce = self.w3.eth.get_transaction_count(self.keeper_address, "pending")

    def _current_gas_price(self, bump_bps: int = 10000) -> int:
        try:
            gp = int(self.w3.eth.gas_price)
        except Exception:
            gp = 1_000_000_000
        bps = max(10000, int(bump_bps))
        return (gp * bps) // 10000

    def get_usdt_transfer_events(self, from_block: int, to_block: int) -> list:
        try:

            logs = self.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": self.usdt_address,
                "topics": [
                    self.TRANSFER_EVENT_TOPIC,
                    None,
                    self._target_to_topic()
                ]
            })
            logger.debug(f"区块 {from_block}-{to_block} 中找到 {len(logs)} 个Transfer事件")
            return logs
        except Exception as e:
            logger.error(f"获取事件失败: {e}")
            return []

    def get_usdt_transfer_events_parallel(self, from_block: int, to_block: int) -> list:
        if to_block < from_block:
            return []

        if (to_block - from_block + 1) <= self.log_chunk_size or self.log_fetch_threads <= 1:
            return self.get_usdt_transfer_events(from_block, to_block)

        futures = []
        start = from_block
        while start <= to_block:
            end = min(to_block, start + self.log_chunk_size - 1)
            futures.append(self._log_executor.submit(self.get_usdt_transfer_events, start, end))
            start = end + 1

        all_logs = []
        for f in as_completed(futures):
            try:
                all_logs.extend(f.result())
            except Exception as e:
                logger.error(f"并发获取事件失败: {e}")
        return all_logs

    def parse_transfer_event(self, event) -> Optional[dict]:
        try:

            topics = event.get('topics') if hasattr(event, "get") else None
            if not topics or len(topics) < 3:
                return None

            t1 = self._to_hex_str(topics[1])
            t2 = self._to_hex_str(topics[2])
            from_address = Web3.to_checksum_address('0x' + t1[-40:])
            to_address = Web3.to_checksum_address('0x' + t2[-40:])

            data_hex = self._to_hex_str(event.get('data'))
            amount = int(data_hex, 16)

            txh = event.get('transactionHash')
            tx_hash = self._to_hex_str(txh)

            bn = event.get('blockNumber')
            if isinstance(bn, str):
                block_number = int(bn, 16)
            else:
                block_number = bn

            return {
                'from': from_address,
                'to': to_address,
                'amount': amount,
                'txHash': tx_hash,
                'blockNumber': block_number
            }
        except Exception as e:
            logger.error(f"解析事件失败: {e}")
            return None

    def is_transfer_processed(self, tx_hash: str) -> bool:
        try:
            tx_hash_bytes = Web3.to_bytes(hexstr=tx_hash)
            return self.controller_contract.functions.processedTransfers(tx_hash_bytes).call()
        except Exception as e:
            logger.error(f"检查处理状态失败: {e}")
            return False

    def process_transfer(self, transfer: dict) -> bool:
        user = transfer['from']
        to = transfer['to']
        amount = transfer['amount']
        tx_hash = transfer['txHash']

        if to.lower() != self.controller_address.lower():
            return False
        if self.pool_seeder_address and user.lower() == self.pool_seeder_address.lower():
            logger.info(
                f"跳过 InitialPoolSeeder 注资转账: from={user}, amount={amount / 10**18} USDT, tx={tx_hash}"
            )
            return False

        if self.is_transfer_processed(tx_hash):
            logger.debug(f"转账 {tx_hash} 已处理，跳过")
            return False

        logger.info(f"发现新入金: 用户={user}, 金额={amount / 10**18} USDT, 交易={tx_hash}")

        if self.witness_enabled:
            queued = self._enqueue_witness_task(transfer)
            if queued:
                logger.info(f"见证任务已等待签名: tx={tx_hash}")
                return True

            with self._witness_lock:
                if tx_hash.lower() in self._witness_tasks:
                    logger.debug(f"见证任务已存在，跳过重复入队: {tx_hash}")
                    return True
            return False

        if not self._witness_disabled_warned:
            logger.warning("见证签名未启用，当前不会提交记账交易；请先配置 WITNESS_SIGNER_ADDRESSES")
            self._witness_disabled_warned = True
        return False

    def _process_transfer_task(self, transfer: dict) -> bool:
        try:
            return self.process_transfer(transfer)
        finally:
            self._inflight_sem.release()

    def run(self, poll_interval: Optional[int] = None):
        if poll_interval is None:
            poll_interval = self.config.get('poll_interval', 12)

        logger.info("开始监听USDT入金...")
        progress_every = int(os.getenv("PROGRESS_LOG_EVERY", "60"))
        last_progress_ts = 0.0

        while True:
            try:

                if self.use_websocket and self.ws_url and self.use_subscription:
                    ok = self._run_ws_subscription_loop()
                    if ok:
                        continue

                    self.use_subscription = False

                latest_block = self.w3.eth.block_number

                safe_head = latest_block - self.confirmations
                if safe_head < 0:
                    safe_head = 0

                now = time.time()
                if progress_every > 0 and (now - last_progress_ts) >= progress_every:
                    mode = "ws-subscribe" if (self.use_websocket and self.ws_url and self.use_subscription) else "http-poll"
                    logger.info(f"[progress] mode={mode} current_block={self.current_block} safe_head={safe_head} latest={latest_block}")
                    last_progress_ts = now

                if safe_head >= self.current_block:

                    scan_end = min(safe_head, self.current_block + self.max_blocks_per_scan)
                    logger.debug(f"扫描区块: {self.current_block} -> {scan_end}")

                    events = self.get_usdt_transfer_events_parallel(self.current_block, scan_end)

                    transfers = []
                    for event in events:
                        t = self.parse_transfer_event(event)
                        if t:
                            transfers.append(t)

                    if transfers:
                        logger.info(f"本段区块共发现 {len(transfers)} 笔候选入金转账")

                    if self.keeper_account and transfers:
                        for t in transfers:
                            self._inflight_sem.acquire()
                            self._tx_executor.submit(self._process_transfer_task, t)
                    else:

                        for t in transfers:
                            self.process_transfer(t)

                    self.current_block = scan_end + 1
                else:

                    time.sleep(poll_interval)

            except KeyboardInterrupt:
                logger.info("监听已停止")
                break
            except Exception as e:
                logger.error(f"监听过程出错: {e}")
                time.sleep(poll_interval)

def load_config() -> dict:
    config = {}

    use_quicknode = os.getenv('USE_QUICKNODE', 'false').lower() == 'true'

    if use_quicknode:

        config['rpc_url'] = os.getenv('QUICKNODE_HTTP_URL', '')
        config['ws_url'] = os.getenv('QUICKNODE_WS_URL', '')

        use_websocket_env = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
        config['use_websocket'] = use_websocket_env and bool(config.get('ws_url'))
        logger.info("使用QuickNode节点")
    else:

        config['rpc_url'] = os.getenv('BSC_RPC_URL', 'https://bsc-dataseed1.binance.org/')
        config['ws_url'] = os.getenv('BSC_WS_URL', '')

        use_websocket_env = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
        config['use_websocket'] = use_websocket_env and bool(config.get('ws_url'))
        logger.info("使用BSC公共节点")

    config['usdt_address'] = os.getenv('USDT_ADDRESS', '0x55d398326f99059fF775485246999027B3197955')
    config['controller_address'] = os.getenv('CONTROLLER_ADDRESS', '')
    config['pool_seeder_address'] = os.getenv('INITIAL_POOL_SEEDER_ADDRESS', '')

    config['keeper_private_key'] = os.getenv('KEEPER_PRIVATE_KEY', '')

    start_block = os.getenv('START_BLOCK', 'latest')
    config['start_block'] = start_block if start_block == 'latest' else int(start_block)
    config['poll_interval'] = int(os.getenv('POLL_INTERVAL', '12'))

    if not config['rpc_url'] or not config['controller_address']:
        if os.path.exists('config.json'):
            logger.info("从config.json加载配置（向后兼容）")
            with open('config.json', 'r') as f:
                json_config = json.load(f)
                if not config['rpc_url']:
                    config['rpc_url'] = json_config.get('rpc_url', 'https://bsc-dataseed1.binance.org/')
                if not config['controller_address']:
                    config['controller_address'] = json_config.get('controller_address', '')
                if not config.get('keeper_private_key'):
                    config['keeper_private_key'] = json_config.get('keeper_private_key', '')
                if config.get('start_block') == 'latest' and json_config.get('start_block'):
                    config['start_block'] = json_config.get('start_block', 'latest')

    return config

def main():

    retry_delay = int(os.getenv("LISTENER_RETRY_DELAY", "10"))
    poll_interval = int(os.getenv("POLL_INTERVAL", "12"))
    while True:
        try:
            load_dotenv(override=True)
            config = load_config()

            if not config.get('controller_address'):
                logger.error("请配置 CONTROLLER_ADDRESS（在 .env 文件或 config.json 中），等待后重试…")
                time.sleep(retry_delay)
                continue

            if not config.get('rpc_url'):
                logger.error("请配置 RPC URL（QUICKNODE_HTTP_URL 或 BSC_RPC_URL），等待后重试…")
                time.sleep(retry_delay)
                continue

            listener = DepositListener(config)
            listener.run(poll_interval=poll_interval)

        except KeyboardInterrupt:
            logger.info("监听已停止")
            break
        except Exception as e:
            logger.error(f"监听初始化/运行失败，将在 {retry_delay}s 后重试: {e}")
            time.sleep(retry_delay)

if __name__ == "__main__":
    main()
