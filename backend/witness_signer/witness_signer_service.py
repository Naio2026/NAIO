import json
import logging
import os
import signal
from threading import Event
from urllib import error, parse, request

from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DOTENV_PATH = os.path.join(_BACKEND_DIR, ".env")
_DOTENV_PATH = os.getenv("WITNESS_SIGNER_DOTENV_PATH", _DEFAULT_DOTENV_PATH)
load_dotenv(dotenv_path=_DOTENV_PATH, override=True)

def _log_level() -> int:
    s = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, s, logging.INFO)

logging.basicConfig(
    level=_log_level(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

class WitnessSignerService:
    def __init__(self):
        self.stop_event = Event()

        self.server_url = os.getenv("WITNESS_HUB_SERVER_URL", "").strip().rstrip("/")
        self.api_key = os.getenv("WITNESS_SIGNER_API_KEY", "").strip()
        self.private_key = self._normalize_private_key(os.getenv("WITNESS_SIGNER_PRIVATE_KEY", ""))
        self.poll_interval = max(1.0, float(os.getenv("WITNESS_SIGNER_POLL_INTERVAL_SECONDS", "2")))
        self.http_timeout = max(3.0, float(os.getenv("WITNESS_SIGNER_HTTP_TIMEOUT_SECONDS", "10")))
        self.expected_address = os.getenv("WITNESS_SIGNER_EXPECTED_ADDRESS", "").strip()

        if not self.server_url:
            raise RuntimeError("WITNESS_HUB_SERVER_URL is required")

        self.signer_address = Account.from_key(self.private_key).address
        if self.expected_address and self.signer_address.lower() != self.expected_address.lower():
            raise RuntimeError(
                "WITNESS_SIGNER_EXPECTED_ADDRESS mismatch: expected=%s actual=%s"
                % (self.expected_address, self.signer_address)
            )

    def _normalize_private_key(self, key: str) -> str:
        k = str(key or "").strip()
        if not k:
            raise RuntimeError("WITNESS_SIGNER_PRIVATE_KEY is required")
        if not k.startswith("0x"):
            k = "0x" + k
        return k

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _http_json(self, method: str, path: str, payload=None) -> dict:
        url = "%s%s" % (self.server_url, path)
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=body, method=method, headers=self._headers())
        try:
            with request.urlopen(req, timeout=self.http_timeout) as resp:
                raw = resp.read().decode("utf-8") if resp else "{}"
                return json.loads(raw) if raw else {}
        except error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                pass
            raise RuntimeError("http %s: %s" % (e.code, raw or e.reason))
        except error.URLError as e:
            raise RuntimeError("network error: %s" % e)

    def _fetch_server_info(self) -> dict:
        return self._http_json("GET", "/v1/info")

    def _fetch_pending(self) -> list:
        q_signer = parse.quote(self.signer_address)
        data = self._http_json("GET", "/v1/pending?signer=%s" % q_signer)
        items = data.get("items") or []
        if not isinstance(items, list):
            return []
        return items

    def _submit_signature(self, tx_hash: str, signature_hex: str) -> dict:
        return self._http_json(
            "POST",
            "/v1/sign",
            {
                "txHash": tx_hash,
                "signer": self.signer_address,
                "signature": signature_hex,
            },
        )

    def _sign_struct_hash(self, struct_hash_hex: str) -> str:
        msg = encode_defunct(hexstr=struct_hash_hex)
        signed = Account.sign_message(msg, self.private_key)
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig

    def _startup_checks(self) -> None:
        info = self._fetch_server_info()
        signers = [str(x).lower() for x in (info.get("witnessSigners") or [])]
        logger.info(
            "hub ok: chainId=%s controller=%s threshold=%s",
            info.get("chainId"),
            info.get("controller"),
            info.get("witnessThreshold"),
        )
        if self.signer_address.lower() not in signers:
            logger.warning("signer not in hub whitelist: %s", self.signer_address)
        else:
            logger.info("signer allowed: %s", self.signer_address)

    def run(self) -> None:
        logger.info("witness signer service started")
        logger.info("server=%s signer=%s", self.server_url, self.signer_address)
        self._startup_checks()

        while not self.stop_event.is_set():
            try:
                items = self._fetch_pending()
                if items:
                    logger.info("pending tasks: %s", len(items))
                for task in items:
                    if self.stop_event.is_set():
                        break
                    tx_hash = str(task.get("txHash") or "")
                    struct_hash = str(task.get("structHash") or "")
                    if not tx_hash or not struct_hash:
                        continue
                    try:
                        sig = self._sign_struct_hash(struct_hash)
                        resp = self._submit_signature(tx_hash, sig)
                        ok = bool(resp.get("ok"))
                        detail = resp.get("detail")
                        if ok:
                            logger.info("signed tx=%s detail=%s", tx_hash, detail)
                        else:
                            logger.warning("submit failed tx=%s detail=%s", tx_hash, detail)
                    except Exception as e:
                        logger.warning("sign/submit failed tx=%s err=%s", tx_hash, e)
            except Exception as e:
                logger.warning("worker cycle failed: %s", e)
            self.stop_event.wait(self.poll_interval)

        logger.info("witness signer service stopped")

    def stop(self) -> None:
        self.stop_event.set()

def main() -> None:
    svc = WitnessSignerService()

    def _sig_handler(_sig, _frm):
        svc.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    svc.run()

if __name__ == "__main__":
    main()
