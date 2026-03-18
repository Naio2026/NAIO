#!/usr/bin/env python3
"""
DApp local server: reads config from .env automatically.

Usage:
  cd /path/to/bsc-naio
  python dapp/server.py

Then open http://localhost:8765/governance.html
(On mobile, same network: http://<host-IP>:8765/governance.html)

Config is read from backend/.env or .env:
  KEEPER_COUNCIL_ADDRESS, CONTROLLER_ADDRESS, BSC_RPC_URL/QUICKNODE_HTTP_URL, CHAIN_ID
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

def _load_dotenv() -> None:
    root = Path(__file__).resolve().parent.parent
    env_paths = [root / "backend" / ".env", root / ".env"]
    try:
        from dotenv import load_dotenv
        for p in env_paths:
            if p.exists():
                load_dotenv(p, override=True)
                return
    except ImportError:
        pass
    for p in env_paths:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k:
                            os.environ.setdefault(k, v)
            return


def _get_config() -> dict:
    _load_dotenv()
    chain_id = 97
    if os.getenv("BSC_TESTNET", "").strip().lower() in ("1", "true", "yes"):
        chain_id = 97
    elif cid := os.getenv("CHAIN_ID", "").strip():
        try:
            chain_id = int(cid)
        except ValueError:
            chain_id = 97
    rpc = (
        os.getenv("QUICKNODE_HTTP_URL", "").strip()
        or os.getenv("BSC_RPC_URL", "").strip()
    )
    if not rpc:
        rpc = "https://data-seed-prebsc-1-s1.binance.org:8545/" if chain_id == 97 else "https://bsc-dataseed.binance.org/"
    return {
        "councilAddress": os.getenv("KEEPER_COUNCIL_ADDRESS", "").strip(),
        "controllerAddress": os.getenv("CONTROLLER_ADDRESS", "").strip(),
        "chainId": chain_id,
        "rpcUrl": rpc,
    }


def main() -> None:
    try:
        from http.server import HTTPServer, SimpleHTTPRequestHandler
    except ImportError:
        print("Python 3 required.")
        sys.exit(1)

    dapp_dir = Path(__file__).resolve().parent
    os.chdir(dapp_dir)

    config = _get_config()

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/config.json" or self.path == "/config":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(config, ensure_ascii=False).encode("utf-8"))
                return
            return SimpleHTTPRequestHandler.do_GET(self)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

    port = int(os.getenv("DAPP_PORT", "8765"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"DApp: http://localhost:{port}/governance.html")
    print(f"Config from .env: council={config['councilAddress'][:20]}... controller={config['controllerAddress'][:20]}...")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
