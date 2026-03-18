#!/usr/bin/env python3
"""
Fill contract/token addresses in docs from a .env file.

Why:
- The real backend/.env usually exists only on the server (gitignored).
- Test docs contain placeholders like <CONTROLLER_ADDRESS>.
- This script updates docs safely without ever writing private keys.

Usage:
  python tools/fill_docs_from_env.py --env backend/.env \
    --docs docs/TESTNET_TESTING_GUIDE.md docs/USER_GUIDE.md

Optional:
  --naio-token 0x...
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path


ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f".env not found: {path}")

    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        out[k] = v
    return out


def _as_address(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    return s if ADDRESS_RE.match(s) else None


@dataclass(frozen=True)
class Addrs:
    controller: str | None
    node_seat_pool: str | None
    naio_token: str | None
    usdt_test_token: str | None


def _resolve_addrs(env: dict[str, str], naio_token_override: str | None) -> Addrs:
    controller = _as_address(env.get("CONTROLLER_ADDRESS"))
    node_seat_pool = _as_address(env.get("NODE_SEAT_POOL_ADDRESS"))

    # NAIO token address is not always in backend/.env; allow multiple keys + override.
    naio_token = _as_address(naio_token_override) or _as_address(env.get("NAIO_TOKEN_ADDRESS")) or _as_address(env.get("NAIOTOKEN_ADDRESS"))

    # Backend listener uses USDT_ADDRESS; deploy script may use USDT_TESTNET. Accept both.
    usdt_test_token = _as_address(env.get("USDT_ADDRESS")) or _as_address(env.get("USDT_TESTNET"))

    return Addrs(
        controller=controller,
        node_seat_pool=node_seat_pool,
        naio_token=naio_token,
        usdt_test_token=usdt_test_token,
    )


def _replace_placeholders(text: str, addrs: Addrs) -> tuple[str, list[str]]:
    repls: list[tuple[str, str | None]] = [
        ("<CONTROLLER_ADDRESS>", addrs.controller),
        ("<NODE_SEAT_POOL_ADDRESS>", addrs.node_seat_pool),
        ("<NAIO_TOKEN_ADDRESS>", addrs.naio_token),
        ("<USDT_TEST_TOKEN_ADDRESS>", addrs.usdt_test_token),
    ]

    changed: list[str] = []
    for ph, val in repls:
        if val and ph in text:
            text = text.replace(ph, val)
            changed.append(f"{ph} -> {val}")
    return text, changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="Path to .env (e.g., backend/.env)")
    ap.add_argument("--docs", nargs="+", required=True, help="Doc files to update")
    ap.add_argument("--naio-token", default=None, help="Override NAIO token address (0x...)")
    ap.add_argument("--backup", action="store_true", help="Write .bak backups for modified docs")
    args = ap.parse_args()

    env_path = Path(args.env)
    docs = [Path(p) for p in args.docs]

    env = _parse_env_file(env_path)
    addrs = _resolve_addrs(env, args.naio_token)

    missing = []
    if not addrs.controller:
        missing.append("CONTROLLER_ADDRESS")
    if not addrs.node_seat_pool:
        missing.append("NODE_SEAT_POOL_ADDRESS")
    if not addrs.usdt_test_token:
        missing.append("USDT_ADDRESS (or USDT_TESTNET)")
    if missing:
        print("WARNING: missing/invalid address keys in .env:", ", ".join(missing))
    if not addrs.naio_token:
        print("NOTE: NAIO token address not found (set NAIO_TOKEN_ADDRESS in .env or pass --naio-token).")

    any_change = False
    for doc in docs:
        if not doc.exists():
            print(f"SKIP (not found): {doc}")
            continue
        before = doc.read_text(encoding="utf-8")
        after, changed = _replace_placeholders(before, addrs)
        if after == before:
            print(f"NOCHANGE: {doc}")
            continue
        if args.backup:
            bak = doc.with_suffix(doc.suffix + ".bak")
            bak.write_text(before, encoding="utf-8")
            print(f"BACKUP: {bak}")
        doc.write_text(after, encoding="utf-8")
        any_change = True
        print(f"UPDATED: {doc}")
        for c in changed:
            print("  -", c)

    if not any_change:
        print("Done. No changes were necessary.")


if __name__ == "__main__":
    main()

