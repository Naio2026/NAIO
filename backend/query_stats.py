from __future__ import annotations

import argparse
import os
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Dict, List

from dotenv import load_dotenv
from web3 import Web3

def _get_epoch_timezone_offset(system_start_ts: int) -> int:
    if system_start_ts == 0:
        return 0

    dt_utc = datetime.utcfromtimestamp(system_start_ts)
    epoch_start_utc = dt_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    epoch_start_ts = int(epoch_start_utc.timestamp())

    offset_seconds = system_start_ts - epoch_start_ts

    offset_hours = round(offset_seconds / 3600)

    if offset_hours > 12:
        offset_hours = offset_hours - 24

    offset_hours = max(-12, min(14, offset_hours))
    return offset_hours

def _get_week_start_end(ts: int, system_start_ts: int = 0) -> tuple[int, int]:
    if system_start_ts > 0:

        offset_hours = _get_epoch_timezone_offset(system_start_ts)

        dt_epoch = datetime.utcfromtimestamp(ts + offset_hours * 3600)
    else:

        dt_epoch = datetime.utcfromtimestamp(ts)
        offset_hours = 0

    days_since_monday = dt_epoch.weekday()
    monday_epoch = dt_epoch - timedelta(days=days_since_monday)
    monday_epoch = monday_epoch.replace(hour=0, minute=0, second=0, microsecond=0)

    next_monday_epoch = monday_epoch + timedelta(days=7)
    sunday_epoch = next_monday_epoch - timedelta(seconds=1)

    monday_utc_ts = int(monday_epoch.timestamp()) - offset_hours * 3600
    sunday_utc_ts = int(sunday_epoch.timestamp()) - offset_hours * 3600

    return monday_utc_ts, sunday_utc_ts

def _get_month_start_end(ts: int, system_start_ts: int = 0) -> tuple[int, int]:
    if system_start_ts > 0:

        offset_hours = _get_epoch_timezone_offset(system_start_ts)

        dt_epoch = datetime.utcfromtimestamp(ts + offset_hours * 3600)
    else:

        dt_epoch = datetime.utcfromtimestamp(ts)
        offset_hours = 0

    month_start_epoch = dt_epoch.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if dt_epoch.month == 12:
        next_month_epoch = month_start_epoch.replace(year=dt_epoch.year + 1, month=1)
    else:
        next_month_epoch = month_start_epoch.replace(month=dt_epoch.month + 1)

    month_end_epoch = next_month_epoch - timedelta(seconds=1)

    month_start_utc_ts = int(month_start_epoch.timestamp()) - offset_hours * 3600
    month_end_utc_ts = int(month_end_epoch.timestamp()) - offset_hours * 3600

    return month_start_utc_ts, month_end_utc_ts

def _get_last_week_start_end(ts: int, system_start_ts: int = 0) -> tuple[int, int]:
    if system_start_ts > 0:

        offset_hours = _get_epoch_timezone_offset(system_start_ts)

        dt_epoch = datetime.utcfromtimestamp(ts + offset_hours * 3600)
    else:

        dt_epoch = datetime.utcfromtimestamp(ts)
        offset_hours = 0

    days_since_monday = dt_epoch.weekday()
    this_monday_epoch = dt_epoch - timedelta(days=days_since_monday)
    this_monday_epoch = this_monday_epoch.replace(hour=0, minute=0, second=0, microsecond=0)

    last_monday_epoch = this_monday_epoch - timedelta(days=7)

    last_sunday_epoch = this_monday_epoch - timedelta(seconds=1)

    last_monday_utc_ts = int(last_monday_epoch.timestamp()) - offset_hours * 3600
    last_sunday_utc_ts = int(last_sunday_epoch.timestamp()) - offset_hours * 3600

    return last_monday_utc_ts, last_sunday_utc_ts

def _get_all_downlines(conn: sqlite3.Connection, user_address: str) -> List[str]:
    downlines = []
    queue = [user_address.lower()]
    visited = set()

    def _table_exists(table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None

    has_referral_relations = _table_exists("referral_relations")
    has_deposit_records = _table_exists("deposit_records")

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        direct_downlines = set()

        if has_referral_relations:
            cursor = conn.execute(
                "SELECT user_address FROM referral_relations WHERE LOWER(referrer_address) = ?",
                (current,),
            )
            for row in cursor:
                if row and row[0]:
                    direct_downlines.add(str(row[0]).lower())

        if has_deposit_records:
            cursor = conn.execute(
                "SELECT DISTINCT user_address FROM deposit_records WHERE LOWER(referrer_address) = ?",
                (current,),
            )
            for row in cursor:
                if row and row[0]:
                    direct_downlines.add(str(row[0]).lower())

        for downline in direct_downlines:
            if downline not in visited:
                downlines.append(downline)
                queue.append(downline)

    return downlines


def _direct_children_for_map(
    conn: sqlite3.Connection, current: str, has_rr: bool, has_dr: bool
) -> List[str]:
    direct: set[str] = set()
    if has_rr:
        cursor = conn.execute(
            "SELECT user_address FROM referral_relations WHERE LOWER(referrer_address) = ?",
            (current,),
        )
        for row in cursor:
            if row and row[0]:
                direct.add(str(row[0]).lower())
    if has_dr:
        cursor = conn.execute(
            "SELECT DISTINCT user_address FROM deposit_records WHERE LOWER(referrer_address) = ?",
            (current,),
        )
        for row in cursor:
            if row and row[0]:
                direct.add(str(row[0]).lower())
    return list(direct)


def query_address_referral_map(db_path: str, root_address: str) -> Dict[str, Any]:
    """
    BFS over the same edges as team stats: referral_relations and deposit_records.referrer.
    Labels each downline with generation depth; deposit totals are sum of amount_wei per user_address.
    """
    root = str(root_address or "").strip().lower()
    if not root.startswith("0x"):
        root = "0x" + root

    if not os.path.isfile(db_path):
        return {"ok": False, "error": "db_not_found", "root": root}

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        has_dr = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deposit_records' LIMIT 1"
            ).fetchone()
            is not None
        )
        has_rr = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='referral_relations' LIMIT 1"
            ).fetchone()
            is not None
        )
        if not has_dr and not has_rr:
            return {"ok": False, "error": "no_tables", "root": root}

        q: deque[tuple[str, int]] = deque([(root, 0)])
        addr_gen: Dict[str, int] = {}
        while q:
            current, depth = q.popleft()
            for ch in _direct_children_for_map(conn, current, has_rr, has_dr):
                if ch == root:
                    continue
                if ch in addr_gen:
                    continue
                addr_gen[ch] = depth + 1
                q.append((ch, depth + 1))

        if not addr_gen:
            return {
                "ok": True,
                "root": root,
                "downline_count": 0,
                "table1": [],
                "table2": [],
                "empty": True,
            }

        addrs = list(addr_gen.keys())
        totals: Dict[str, int] = defaultdict(int)
        if has_dr and addrs:
            placeholders = ",".join(["?"] * len(addrs))
            cur = conn.execute(
                f"SELECT user_address, amount_wei FROM deposit_records WHERE user_address IN ({placeholders})",
                addrs,
            )
            for row in cur:
                u = str(row[0]).lower() if row[0] else ""
                if not u:
                    continue
                try:
                    totals[u] += int(row[1]) if row[1] not in (None, "") else 0
                except (ValueError, TypeError):
                    continue

        by_gen: Dict[int, Dict[str, int]] = {}
        for addr, g in addr_gen.items():
            if g not in by_gen:
                by_gen[g] = {"count": 0, "layer_total_wei": 0}
            by_gen[g]["count"] += 1
            by_gen[g]["layer_total_wei"] += int(totals.get(addr, 0))

        max_g = max(by_gen.keys())
        table1: List[Dict[str, Any]] = []
        cum = 0
        for g in range(1, max_g + 1):
            if g not in by_gen:
                continue
            row = by_gen[g]
            lt = int(row["layer_total_wei"])
            cum += lt
            table1.append(
                {
                    "generation": g,
                    "count": int(row["count"]),
                    "layer_total_wei": lt,
                    "cumulative_wei": cum,
                }
            )

        table2: List[Dict[str, Any]] = []
        for addr in sorted(addr_gen.keys(), key=lambda x: (addr_gen[x], x)):
            table2.append(
                {
                    "address": addr,
                    "generation": addr_gen[addr],
                    "user_total_wei": int(totals.get(addr, 0)),
                }
            )

        return {
            "ok": True,
            "root": root,
            "downline_count": len(addr_gen),
            "table1": table1,
            "table2": table2,
            "empty": False,
        }
    finally:
        conn.close()


def query_downline_deposits(db_path: str, user_address: str, period: str = "all", system_start_ts: int = 0) -> Dict:
    user_address = user_address.lower()
    conn = sqlite3.connect(db_path, timeout=30)

    try:

        has_deposit_records = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deposit_records' LIMIT 1"
        ).fetchone() is not None

        downlines = _get_all_downlines(conn, user_address)

        timezone_offset = _get_epoch_timezone_offset(system_start_ts) if system_start_ts > 0 else 0

        if (not has_deposit_records) or (not downlines):
            return {
                "user": user_address,
                "period": period,
                "downline_count": 0,
                "total_deposit_wei": 0,
                "total_deposit_usdt": 0.0,
                "start_ts": None,
                "end_ts": None,
                "timezone_offset": timezone_offset
            }

        now_ts = int(time.time())
        start_ts = None
        end_ts = None

        if period == "week":
            start_ts, end_ts = _get_week_start_end(now_ts, system_start_ts)
        elif period == "last_week":
            start_ts, end_ts = _get_last_week_start_end(now_ts, system_start_ts)
        elif period == "month":
            start_ts, end_ts = _get_month_start_end(now_ts, system_start_ts)

        if start_ts is not None and end_ts is not None:
            query = (
                "SELECT amount_wei FROM deposit_records WHERE user_address IN ({}) "
                "AND block_timestamp >= ? AND block_timestamp <= ?"
            ).format(",".join(["?"] * len(downlines)))
            params = downlines + [start_ts, end_ts]
        else:
            query = (
                "SELECT amount_wei FROM deposit_records WHERE user_address IN ({})"
            ).format(",".join(["?"] * len(downlines)))
            params = downlines

        cursor = conn.execute(query, params)

        total_wei = 0
        for row in cursor:
            amount_str = row[0]
            if amount_str:
                try:
                    total_wei += int(amount_str)
                except (ValueError, TypeError):
                    continue

        return {
            "user": user_address,
            "period": period,
            "downline_count": len(downlines),
            "total_deposit_wei": total_wei,
            "total_deposit_usdt": total_wei / 1e18,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "timezone_offset": timezone_offset
        }
    finally:
        conn.close()

def main():
    ap = argparse.ArgumentParser(description="Query user team performance stats")
    ap.add_argument("--user", required=True, help="User address")
    ap.add_argument("--period", default="all", choices=["all", "week", "last_week", "month"],
                    help="Period: all=all time, week=this week, last_week=last week, month=this month")
    ap.add_argument("--db", help="Database path (overrides PRICE_DB_PATH)")
    ap.add_argument("--system-start-ts", type=int, default=0, help="systemStartTs used to infer epoch timezone (0 means UTC)")
    args = ap.parse_args()

    load_dotenv(override=True)

    db_path = args.db or os.getenv("PRICE_DB_PATH", "price_history.db").strip() or "price_history.db"

    if not os.path.exists(db_path):
        print(f"Error: DB file not found: {db_path}")
        return 1

    try:
        user_address = Web3.to_checksum_address(args.user)
    except Exception as e:
        print(f"Error: invalid address format: {e}")
        return 1

    result = query_downline_deposits(db_path, user_address, args.period, args.system_start_ts)

    print(f"\nUser: {result['user']}")
    print(f"Period: {result['period']}")
    print(f"Downline count: {result['downline_count']}")
    print(f"Total: {result['total_deposit_usdt']:.2f} USDT")

    timezone_offset = result.get('timezone_offset', 0)
    if timezone_offset != 0:
        tz_sign = "+" if timezone_offset >= 0 else ""
        print(f"Timezone: UTC{tz_sign}{timezone_offset} (epoch-based)")
    else:
        print("Timezone: UTC (default)")

    if result['start_ts'] and result['end_ts']:
        start_dt = datetime.utcfromtimestamp(result['start_ts'])
        end_dt = datetime.utcfromtimestamp(result['end_ts'])
        tz_str = f"UTC{tz_sign}{timezone_offset}" if timezone_offset != 0 else "UTC"
        print(f"Time range ({tz_str}): {start_dt.strftime('%Y-%m-%d %H:%M:%S')} to {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
