#!/usr/bin/env python3
"""
Generate 1000 EOA addresses + private keys for NodeSeatPool initialization.

Outputs (repo root):
  - nodes_list.txt      : 1000 lines, each a checksummed address
  - nodes_keys.json     : [{"index":0,"address":"0x..","private_key":"0x.."}, ...]
  - nodes_keys.csv      : index,address,private_key

Security:
  - DO NOT commit or share nodes_keys.* files.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
from pathlib import Path


def _require_eth_account():
    try:
        from eth_account import Account  # type: ignore
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            "Missing dependency: eth-account.\n"
            "Install it with:\n"
            "  pip install -r backend/requirements.txt\n"
        ) from e
    return Account


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate node addresses list + keys for testing.")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--out-dir", type=str, default=".")
    parser.add_argument("--seed-hex", type=str, default="", help="Optional 32-byte hex seed for deterministic generation (DANGER).")
    args = parser.parse_args()

    if args.count <= 0:
        raise SystemExit("--count must be > 0")

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve repo root (assume this script lives in tools/)
    repo_root = (Path(__file__).resolve().parent.parent)
    # If out-dir is relative, default to repo root
    if args.out_dir == ".":
        out_dir = repo_root

    Account = _require_eth_account()
    Account.enable_unaudited_hdwallet_features()  # no-op for our usage, safe

    # Deterministic option (for reproducible tests) - uses HMAC-like stream via sha256.
    # Default is cryptographically random keys.
    seed = None
    if args.seed_hex:
        seed_hex = args.seed_hex.lower().removeprefix("0x")
        if len(seed_hex) != 64:
            raise SystemExit("--seed-hex must be 32 bytes hex (64 chars)")
        seed = bytes.fromhex(seed_hex)

    results: list[dict] = []
    seen = set()

    def next_key(i: int) -> bytes:
        if seed is None:
            return secrets.token_bytes(32)
        import hashlib

        return hashlib.sha256(seed + i.to_bytes(4, "big")).digest()

    i = 0
    while len(results) < args.count:
        pk_bytes = next_key(i)
        i += 1
        acct = Account.from_key(pk_bytes)
        addr = acct.address  # checksummed
        if addr in seen:
            continue
        seen.add(addr)
        results.append(
            {
                "index": len(results),
                "address": addr,
                "private_key": "0x" + pk_bytes.hex(),
            }
        )

    nodes_list_path = out_dir / "nodes_list.txt"
    keys_json_path = out_dir / "nodes_keys.json"
    keys_csv_path = out_dir / "nodes_keys.csv"

    # Refuse to overwrite existing files unless explicitly removed by user
    for p in (nodes_list_path, keys_json_path, keys_csv_path):
        if p.exists():
            raise SystemExit(f"Refusing to overwrite existing file: {p}")

    nodes_list_path.write_text("\n".join([r["address"] for r in results]) + "\n", encoding="utf-8")
    keys_json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    with keys_csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["index", "address", "private_key"])
        for r in results:
            w.writerow([r["index"], r["address"], r["private_key"]])

    # Print brief summary for user
    print(f"Wrote {len(results)} addresses to: {nodes_list_path}")
    print(f"Wrote private keys to: {keys_json_path}")
    print(f"Wrote private keys to: {keys_csv_path}")
    print("SECURITY: Do NOT commit/share nodes_keys.*")


if __name__ == "__main__":
    main()

