import argparse
import sqlite3
from typing import Set, List

def _load_all_deposit_users(conn: sqlite3.Connection) -> Set[str]:
    users: Set[str] = set()
    cursor = conn.execute("SELECT DISTINCT user_address FROM deposit_records")
    for row in cursor:
        addr = (row[0] or "").strip().lower()
        if addr:
            users.add(addr)
    return users

def _load_all_referral_users(conn: sqlite3.Connection) -> Set[str]:
    users: Set[str] = set()
    cursor = conn.execute("SELECT DISTINCT user_address FROM referral_relations")
    for row in cursor:
        addr = (row[0] or "").strip().lower()
        if addr:
            users.add(addr)
    return users

def _get_downlines(conn: sqlite3.Connection, root: str) -> Set[str]:
    root = (root or "").strip().lower()
    if not root:
        return set()

    downlines: Set[str] = set()
    queue: List[str] = [root]
    visited: Set[str] = set()

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        cursor = conn.execute(
            "SELECT user_address FROM referral_relations WHERE referrer_address = ?",
            (current,),
        )
        for row in cursor:
            addr = (row[0] or "").strip().lower()
            if not addr or addr in visited:
                continue
            downlines.add(addr)
            queue.append(addr)

    return downlines

def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze deposit users missing referral relations")
    ap.add_argument("--db", required=True, help="DB path (e.g., price_history.db)")
    ap.add_argument(
        "--root",
        help="Optional: root address; only analyze its downlines (recursive via referral_relations)",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    try:

        def _table_exists(name: str) -> bool:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (name,),
            ).fetchone()
            return row is not None

        if not _table_exists("deposit_records"):
            print("Table deposit_records does not exist; cannot analyze.")
            return 1
        if not _table_exists("referral_relations"):
            print("Table referral_relations does not exist; cannot analyze missing referrals.")
            return 1

        all_deposit_users = _load_all_deposit_users(conn)
        all_referral_users = _load_all_referral_users(conn)

        if args.root:
            root_downlines = _get_downlines(conn, args.root)
            target_users = all_deposit_users.intersection(root_downlines)
            print(f"Within downlines of root {args.root}:")
        else:
            target_users = all_deposit_users
            print("Global scope:")

        missing = sorted(
            [u for u in target_users if u not in all_referral_users]
        )

        print(f"- Total deposit users: {len(all_deposit_users)}")
        print(f"- Users with referral relation: {len(all_referral_users)}")
        print(f"- Deposit users in scope: {len(target_users)}")
        print(f"- Missing referral relation in scope: {len(missing)}")
        print("")

        if not missing:
            print('No users found with "deposit but missing referral relation".')
            return 0

        print("The following users have deposits in deposit_records but no entry in referral_relations:")
        for addr in missing:
            print(addr)

        return 0
    finally:
        conn.close()

if __name__ == "__main__":
    raise SystemExit(main())
