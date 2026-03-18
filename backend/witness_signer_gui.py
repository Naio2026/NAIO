import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional
from urllib import error, parse, request

from eth_account import Account
from eth_account.messages import encode_defunct

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "witness_signer_gui.config.json")

class WitnessSignerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("NAIO Witness Signer")
        self.root.geometry("860x620")

        self.server_url_var = tk.StringVar(value="http://127.0.0.1:8787")
        self.api_key_var = tk.StringVar(value="")
        self.private_key_var = tk.StringVar(value="")
        self.poll_interval_var = tk.StringVar(value="2")
        self.signer_addr_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="stopped")

        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._load_config()
        self._refresh_signer_address()
        self._schedule_log_flush()

    def _build_ui(self) -> None:
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        row = 0
        ttk.Label(frm, text="Server URL").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.server_url_var, width=72).grid(row=row, column=1, sticky="we", padx=8)
        row += 1

        ttk.Label(frm, text="API Key").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.api_key_var, width=72).grid(row=row, column=1, sticky="we", padx=8)
        row += 1

        ttk.Label(frm, text="Private Key").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.private_key_var, width=72, show="*").grid(row=row, column=1, sticky="we", padx=8)
        row += 1

        ttk.Label(frm, text="Poll Interval (s)").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.poll_interval_var, width=12).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(frm, text="Signer Address").grid(row=row, column=0, sticky="w")
        ttk.Label(frm, textvariable=self.signer_addr_var).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(frm, text="Status").grid(row=row, column=0, sticky="w")
        ttk.Label(frm, textvariable=self.status_var).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 10))
        ttk.Button(btns, text="Refresh Signer", command=self._refresh_signer_address).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Save Config", command=self._save_config).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Test Server", command=self._test_server).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Fetch Once", command=self._fetch_once).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Start", command=self._start_worker).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Stop", command=self._stop_worker).pack(side="left")
        row += 1

        self.log_text = scrolledtext.ScrolledText(frm, height=24)
        self.log_text.grid(row=row, column=0, columnspan=2, sticky="nsew")
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(row, weight=1)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log_queue.put(f"{ts} | {msg}")

    def _schedule_log_flush(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.root.after(300, self._schedule_log_flush)

    def _load_config(self) -> None:
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.server_url_var.set(str(data.get("server_url") or self.server_url_var.get()))
            self.api_key_var.set(str(data.get("api_key") or ""))
            self.private_key_var.set(str(data.get("private_key") or ""))
            self.poll_interval_var.set(str(data.get("poll_interval") or self.poll_interval_var.get()))
        except Exception as e:
            self._log(f"load config failed: {e}")

    def _save_config(self) -> None:
        data = {
            "server_url": self.server_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "private_key": self.private_key_var.get().strip(),
            "poll_interval": self.poll_interval_var.get().strip(),
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._log(f"config saved: {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror("Save Config", f"save failed: {e}")

    def _normalize_private_key(self, key: str) -> str:
        k = key.strip()
        if not k:
            raise ValueError("empty private key")
        if not k.startswith("0x"):
            k = "0x" + k
        return k

    def _derive_signer_address(self) -> str:
        pk = self._normalize_private_key(self.private_key_var.get())
        return Account.from_key(pk).address

    def _refresh_signer_address(self) -> None:
        try:
            addr = self._derive_signer_address()
            self.signer_addr_var.set(addr)
            self._log(f"signer: {addr}")
        except Exception as e:
            self.signer_addr_var.set("-")
            self._log(f"invalid private key: {e}")

    def _headers(self, api_key: Optional[str] = None) -> dict:
        h = {"Content-Type": "application/json"}
        key = (api_key if api_key is not None else self.api_key_var.get()).strip()
        if key:
            h["X-Api-Key"] = key
        return h

    def _http_json(self, method: str, path: str, payload: Optional[dict] = None, timeout: int = 10) -> dict:
        return self._http_json_with(
            server_url=self.server_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            method=method,
            path=path,
            payload=payload,
            timeout=timeout,
        )

    def _http_json_with(
        self,
        server_url: str,
        api_key: str,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        timeout: int = 10,
    ) -> dict:
        base = server_url.strip().rstrip("/")
        if not base:
            raise ValueError("empty server url")
        url = f"{base}{path}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=body, method=method, headers=self._headers(api_key=api_key))
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") if resp else "{}"
                return json.loads(raw) if raw else {}
        except error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                pass
            raise RuntimeError(f"http {e.code}: {raw or e.reason}") from e
        except error.URLError as e:
            raise RuntimeError(f"network error: {e}") from e

    def _test_server(self) -> None:
        try:
            info = self._http_json("GET", "/v1/info")
            self._log(
                "server ok: chainId=%s controller=%s threshold=%s"
                % (
                    info.get("chainId"),
                    info.get("controller"),
                    info.get("witnessThreshold"),
                )
            )
            try:
                addr = self._derive_signer_address()
                signers = [str(x).lower() for x in (info.get("witnessSigners") or [])]
                if addr.lower() in signers:
                    self._log(f"signer allowed by server: {addr}")
                else:
                    self._log(f"WARNING signer not in server whitelist: {addr}")
            except Exception:
                pass
        except Exception as e:
            self._log(f"test server failed: {e}")

    def _fetch_pending(self, signer: str) -> list[dict]:
        q_signer = parse.quote(signer)
        data = self._http_json("GET", f"/v1/pending?signer={q_signer}")
        items = data.get("items") or []
        if not isinstance(items, list):
            return []
        return items

    def _fetch_pending_with(self, server_url: str, api_key: str, signer: str) -> list[dict]:
        q_signer = parse.quote(signer)
        data = self._http_json_with(server_url, api_key, "GET", f"/v1/pending?signer={q_signer}")
        items = data.get("items") or []
        if not isinstance(items, list):
            return []
        return items

    def _post_signature(self, tx_hash: str, signer: str, signature_hex: str) -> dict:
        return self._http_json(
            "POST",
            "/v1/sign",
            {
                "txHash": tx_hash,
                "signer": signer,
                "signature": signature_hex,
            },
        )

    def _post_signature_with(
        self,
        server_url: str,
        api_key: str,
        tx_hash: str,
        signer: str,
        signature_hex: str,
    ) -> dict:
        return self._http_json_with(
            server_url,
            api_key,
            "POST",
            "/v1/sign",
            {
                "txHash": tx_hash,
                "signer": signer,
                "signature": signature_hex,
            },
        )

    def _sign_struct_hash(self, private_key: str, struct_hash_hex: str) -> str:
        msg = encode_defunct(hexstr=struct_hash_hex)
        signed = Account.sign_message(msg, private_key)
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig

    def _fetch_once(self) -> None:
        try:
            signer = self._derive_signer_address()
            items = self._fetch_pending(signer)
            self._log(f"pending={len(items)}")
        except Exception as e:
            self._log(f"fetch once failed: {e}")

    def _start_worker(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self._log("worker already running")
            return
        try:
            server_url = self.server_url_var.get().strip()
            api_key = self.api_key_var.get().strip()
            private_key = self._normalize_private_key(self.private_key_var.get())
            signer = Account.from_key(private_key).address
            interval = max(1.0, float(self.poll_interval_var.get().strip() or "2"))
        except Exception as e:
            messagebox.showerror("Start", f"invalid config: {e}")
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(server_url, api_key, private_key, signer, interval),
            daemon=True,
        )
        self.worker_thread.start()
        self.status_var.set("running")
        self._log(f"worker started signer={signer}")

    def _stop_worker(self) -> None:
        self.stop_event.set()
        self.status_var.set("stopped")
        self._log("worker stopping...")

    def _worker_loop(
        self,
        server_url: str,
        api_key: str,
        private_key: str,
        signer: str,
        interval: float,
    ) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    items = self._fetch_pending_with(server_url, api_key, signer)
                    if items:
                        self._log(f"fetched pending tasks: {len(items)}")
                    for task in items:
                        if self.stop_event.is_set():
                            break
                        tx_hash = str(task.get("txHash") or "")
                        struct_hash = str(task.get("structHash") or "")
                        if not tx_hash or not struct_hash:
                            continue
                        try:
                            sig = self._sign_struct_hash(private_key, struct_hash)
                            rsp = self._post_signature_with(server_url, api_key, tx_hash, signer, sig)
                            ok = bool(rsp.get("ok"))
                            detail = rsp.get("detail")
                            if ok:
                                self._log(f"signed tx={tx_hash} detail={detail}")
                            else:
                                self._log(f"submit failed tx={tx_hash} detail={detail}")
                        except Exception as e:
                            self._log(f"sign/submit failed tx={tx_hash}: {e}")
                except Exception as e:
                    self._log(f"worker fetch failed: {e}")
                self.stop_event.wait(interval)
        finally:
            self.root.after(0, lambda: self.status_var.set("stopped"))
            self._log("worker stopped")

    def _on_close(self) -> None:
        self._stop_worker()
        self.root.after(250, self.root.destroy)

def main() -> None:
    root = tk.Tk()
    app = WitnessSignerGUI(root)
    app._log("NAIO witness signer GUI ready")
    root.mainloop()

if __name__ == "__main__":
    main()
